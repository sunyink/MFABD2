"""掉落播报读取的动作层：连拍时只截图不识别，识别统一留到列表停下以后。

    LootFeedCast     点前序识别框，然后连拍一段时间（替代施放节点原来的点击 + post_delay）
    LootFeedWatch    只连拍，不点击（别的场景想盯这条列表时用）
    LootFeedCollect  等列表停下，把还没读过的条目一次识别出来并播报

为什么不边拍边识别：识别一行要 0.15–0.4 秒（游戏同机运行时偏慢），而条目 0.2 秒来一条、
最快 1.6 秒就被挤出屏幕；截图只要几十毫秒。连拍期间不识别，读得全不全就与机器快慢无关。
跟踪与拼行的做法见 utils/loot_feed.py。

参数（custom_action_param，全部可省）：
    feed       会话名，同一任务内同名的动作共用一份跟踪状态。默认 "default"
    switch     LootFeedCast：一个节点名，它启用时才连拍，否则只点击并等 watch_ms（与原节点等价）
    tag        LootFeedCast：记在这次点击之后到达的条目上（如 "吸取"）
    watch_ms   Cast/Watch：至少连拍多久。默认 2000
    confirm    LootFeedCast：false 时不判「游戏确认」（见下）。默认 true
    after_ms   LootFeedCast：游戏确认之后至少再拍这么久。默认 1000
    max_ms     连拍或等待的上限。Cast/Watch 默认 5000，Collect 默认 4000
    grace_ms   Collect：最后一次施放被游戏确认后至少这么久才认为不会再来；没看到确认的从收取开始算。默认 1000
    settle_ms  Collect：列表这么久没有新动静才算停。默认 500
    label      Collect：写进结果与日志的标签

游戏确认：点下去到游戏真正放出技能隔多久不一定（实测 0.85–2.5 秒，探查生效时吸取的前摇更长），
列表在确认后 0.15–0.3 秒才开始出。LootFeedCast 连拍时拿每一帧重跑本节点自己的识别：按钮不再可点
（识别连续 0.3 秒不命中）就是确认了，从那一刻起再拍 after_ms。窗口内没等到确认就照常返回，
由流程回到分流节点重判——与原来「等 2 秒再判」一致。

版式（底框位置、格距…）读节点 Agt_LootFeed_Layout 的 attach，覆盖包改那个节点即可。
"""

import json
import random
import time
from functools import lru_cache
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import loot_feed, mfaalog
from utils.ocr_item_name import clean, resolve_ocr_name

LAYOUT_NODE = "Agt_LootFeed_Layout"
OCR_NODE = "Agt_LootFeed_Line_Ocr"
FRAME_PERIOD = 0.09      # 连拍周期（秒）。出条间隔 0.2 秒，离线回放在 0.3 秒间隔下仍全对
READ_BUDGET = 15.0       # 一次 Collect 里识别最多花这么久，超了剩下的记为没读成
CONFIRM_STREAK = 0.3     # 按下时按钮会闪约 0.15 秒，识别要连续这么久不命中才算技能已被吃掉


def _params(argv):
    raw = argv.custom_action_param
    if not raw:
        return {}
    value = raw if isinstance(raw, dict) else json.loads(str(raw).strip())
    return value if isinstance(value, dict) else {}


def _task_id(argv):
    return getattr(getattr(argv, "task_detail", None), "task_id", 0)


def _session(context, argv, feed):
    def make_layout():
        try:
            attach = dict(getattr(context.get_node_object(LAYOUT_NODE), "attach", None) or {})
        except Exception:
            attach = {}
        return loot_feed.layout_from(attach)
    return loot_feed.open_session(_task_id(argv), feed, make_layout)


def _switched_on(context, params):
    """`switch` 指向一个节点：它启用时才连拍。没写 switch 就一直连拍。"""
    node = params.get("switch")
    if not node:
        return True
    try:
        return bool(getattr(context.get_node_object(str(node)), "enabled", False))
    except Exception:
        return False


def _grab(context):
    job = context.tasker.controller.post_screencap().wait()
    if not job.succeeded:
        return None          # 截图失败时 get() 可能给出上一张旧图，旧图会让条目看起来赖在屏上
    img = job.get()
    return img if img is not None and getattr(img, "ndim", 0) == 3 else None


def _watch(context, session, min_s, max_s, hold=None, on_frame=None):
    """连拍并喂给跟踪器。过了 min_s 且列表不在出条（且 hold 不再要求）就停，最迟 max_s。"""
    start = time.monotonic()
    while not context.tasker.stopping:
        t0 = time.monotonic()
        img = _grab(context)
        now = time.monotonic()
        if img is not None:
            session.tracker.feed(now, img)
            session.frames += 1
            if on_frame:
                on_frame(now, img)
        elapsed = now - start
        if elapsed >= max_s:
            break
        if elapsed >= min_s and not session.tracker.busy(now) and not (hold and hold(now)):
            break
        rest = FRAME_PERIOD - (time.monotonic() - t0)
        if rest > 0:
            time.sleep(rest)


def _watch_safely(context, session, min_s, max_s, hold=None, on_frame=None):
    """连拍出任何错都退化成原来的固定等待，不影响施放流程。"""
    start = time.monotonic()
    try:
        _watch(context, session, min_s, max_s, hold, on_frame)
    except Exception as e:
        mfaalog.error(f"[LootFeed] 连拍出错，退回固定等待: {e!r}")
        rest = min_s - (time.monotonic() - start)
        if rest > 0:
            time.sleep(rest)


@lru_cache(maxsize=1)
def _aliases():
    """全部物品的简繁名 → 简体名。掉落里有货币、兑换币这类不在商店品类里的东西，所以不按品类筛。"""
    data = Path(__file__).resolve().parents[1] / "data"
    items = json.loads((data / "bd2_item_names_i18n.json").read_text(encoding="utf-8"))
    items += json.loads((data / "bd2_item_names_extra.json").read_text(encoding="utf-8"))["items"]
    aliases = {clean(item[key]): item["cn"] for item in items if item.get("cn")
               for key in ("cn", "tw") if item.get(key)}
    known = set(aliases.values())
    norm = json.loads((data / "bd2_name_norm_tw2cn.json").read_text(encoding="utf-8"))
    aliases.update({clean(k): v for k, v in norm.items() if v in known})
    return aliases


def _resolve_name(raw, score):
    try:
        found = resolve_ocr_name(raw, score, aliases=_aliases())
        return found["name"], bool(found["confirmed"])
    except Exception as e:
        mfaalog.warning(f"[LootFeed] 物品字典不可用，名字按原文记: {e!r}")
        return raw, False


def _ocr_line(context, canvas, roi):
    detail = context.run_recognition(OCR_NODE, canvas, {OCR_NODE: {"roi": roi}})
    if detail is None:
        raise RuntimeError(f"OCR 节点未能执行: {OCR_NODE}")
    items = getattr(detail, "filtered_results", None) or getattr(detail, "all_results", None) or []
    texts = [(str(getattr(i, "text", "") or ""), float(getattr(i, "score", 0.0) or 0.0)) for i in items]
    texts = [t for t in texts if t[0]]
    return "".join(t for t, _ in texts), min((s for _, s in texts), default=0.0)


def _read(context, tracker, entries):
    """三轮读法，后一轮只读前一轮没读成的：合成条拼行 → 合成条不截断单读 → 原始条单读。"""
    readable = [e for e in entries if e.crops]
    result, calls, start = {}, 0, time.monotonic()
    plans = (("packed", tracker.clean_strip, None), ("single", tracker.clean_strip, 0), ("raw", tracker.raw_strip, 0))
    for attempt, (mode, pick, max_gap) in enumerate(plans):
        todo = [e for e in readable if e.idx not in result]
        if not todo:
            break
        for group in ([todo] if mode == "packed" else [[e] for e in todo]):
            canvas, lines = loot_feed.pack_lines([pick(e) for e in group], max_gap=max_gap)
            for roi, members in lines:
                if context.tasker.stopping or time.monotonic() - start > READ_BUDGET:
                    return result, calls, time.monotonic() - start
                text, score = _ocr_line(context, canvas, roi)
                calls += 1
                pieces = loot_feed.split_line(text, len(members))
                if pieces is None:
                    continue           # 段数对不上：整行不认，留给下一轮逐条读
                for i, piece in zip(members, pieces):
                    row = loot_feed.parse_row(piece)
                    if row:
                        result[group[i].idx] = (row[0], row[1], score, attempt)
    return result, calls, time.monotonic() - start


def _collect(context, session, feed, params):
    tracker = session.tracker
    grace = float(params.get("grace_ms", 1000)) / 1000
    settle = float(params.get("settle_ms", 500)) / 1000
    max_s = float(params.get("max_ms", 4000)) / 1000
    waited = time.monotonic()
    # 流程走到出口时技能都已被确认；没在连拍里看到确认的，最晚就是此刻刚确认
    last = session.casts[-1] if session.casts else None
    since = None if last is None else (last["confirmed"] if last["confirmed"] is not None else waited)

    def hold(now):
        if since is not None and now - since < grace:
            return True
        latest = max(t for t in (tracker.t_last_new, tracker.t_last_glow, float("-inf")) if t is not None)
        return now - latest < settle or tracker.unsettled(now) > 0

    _watch(context, session, 0.0, max_s, hold)
    waited = time.monotonic() - waited

    entries = [e for e in tracker.ordered() if not e.taken]
    result, calls, read_s = _read(context, tracker, entries)
    items, unread = [], 0
    for e in entries:
        got = result.get(e.idx)
        e.taken, e.crops = True, []
        if got is None:
            unread += 1
            continue
        raw, count, score, attempt = got
        name, known = _resolve_name(raw, score)
        after = next((c["tag"] for c in reversed(session.casts) if c["t"] <= e.t_first), None)
        items.append({"name": name, "raw": raw, "count": count, "score": round(score, 4), "known": known,
                      "after": after, "reread": attempt, "frames": e.seen, "occluded": e.sig_changed})
    resync = sum(1 for n in tracker.notes if n[1] == "resync")
    tracker.notes.clear()
    record = {"time": time.time(), "feed": feed, "label": params.get("label"), "items": items,
              "unread": unread, "resync": resync}
    confirms = "、".join(f"{c['tag'] or '?'} " + ("未见确认" if c["confirmed"] is None else f"{(c['confirmed'] - c['t']) * 1000:.0f}ms")
                        for c in session.casts)
    session.casts.clear()
    loot_feed.add_record(feed, record)

    label = f"[{record['label']}]" if record["label"] else ""
    if items or unread:
        text = "、".join(f"{i['name']}×{i['count']}" for i in items) or "无"
        tail = f"；另有 {unread} 条没读成" if unread else ""
        unknown = [i["raw"] for i in items if not i["known"]]
        tail += f"；字典里没有: {'、'.join(unknown)}" if unknown else ""
        mfaalog.info(f"[LootFeed]{label} 掉落 {len(items)} 条: {text}{tail}")
    mfaalog.debug(f"[LootFeed]{label} 连拍累计 {session.frames} 帧，收尾等待 {waited * 1000:.0f}ms，"
                  f"识别 {calls} 次 {read_s * 1000:.0f}ms，重同步 {resync} 次；点击到确认: {confirms or '无'}")
    session.frames = 0
    return record


def _click_point(box):
    """前序识别框中部随机一点。"""
    if hasattr(box, "x"):
        x, y, w, h = box.x, box.y, box.w, box.h
    else:
        x, y, w, h = box
    return (int(x + w * random.uniform(0.3, 0.7)), int(y + h * random.uniform(0.3, 0.7)))


@AgentServer.custom_action("LootFeedCast")
class LootFeedCastAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _params(argv)
            watch_s = float(params.get("watch_ms", 2000)) / 1000
            x, y = _click_point(argv.box)
            clicked = context.tasker.controller.post_click(x, y).wait().succeeded
        except Exception as e:
            mfaalog.error(f"[LootFeed] LootFeedCast 点击失败: {e!r}")
            return False
        session = None
        try:
            if _switched_on(context, params):
                session = _session(context, argv, str(params.get("feed", "default")))
        except Exception as e:
            mfaalog.error(f"[LootFeed] 会话建立失败，本次只点击不连拍: {e!r}")
        if session is None:
            time.sleep(watch_s)          # 没开记录：与原来的点击 + post_delay 等价
            return bool(clicked)

        cast = {"t": time.monotonic(), "tag": params.get("tag"), "confirmed": None}
        session.casts.append(cast)
        node = argv.node_name if params.get("confirm", True) else None
        after_s = float(params.get("after_ms", 1000)) / 1000
        miss_since = None

        def on_frame(now, img):
            nonlocal miss_since
            if not node or cast["confirmed"] is not None:
                return
            detail = context.run_recognition(node, img)
            if detail is None:
                return
            if detail.hit:
                miss_since = None
            elif miss_since is None:
                miss_since = now
            elif now - miss_since >= CONFIRM_STREAK:
                cast["confirmed"] = miss_since

        def hold(now):
            return cast["confirmed"] is not None and now - cast["confirmed"] < after_s

        _watch_safely(context, session, watch_s, float(params.get("max_ms", 5000)) / 1000, hold, on_frame)
        return bool(clicked)


@AgentServer.custom_action("LootFeedWatch")
class LootFeedWatchAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _params(argv)
            session = _session(context, argv, str(params.get("feed", "default")))
        except Exception as e:
            mfaalog.error(f"[LootFeed] LootFeedWatch 参数或会话出错: {e!r}")
            return True
        _watch_safely(context, session, float(params.get("watch_ms", 2000)) / 1000,
                      float(params.get("max_ms", 5000)) / 1000)
        return True


@AgentServer.custom_action("LootFeedCollect")
class LootFeedCollectAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 读不出来只少一份记录，不该拦住出口
        try:
            params = _params(argv)
            feed = str(params.get("feed", "default"))
            _collect(context, _session(context, argv, feed), feed, params)
        except Exception as e:
            mfaalog.error(f"[LootFeed] LootFeedCollect 出错，本次掉落未记录: {e!r}")
        return True
