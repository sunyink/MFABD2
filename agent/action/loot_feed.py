"""掉落播报读取的动作层：连拍时只截图不识别，识别统一留到列表停下以后。

    LootFeedCast     点前序识别框，然后连拍一段时间（替代施放节点原来的点击 + post_delay）
    LootFeedWatch    只连拍，不点击（落地时补拍；别的场景想盯这条列表时也用它）
    LootFeedConfirm  流程认出某个技能已用掉时，把它最近一次施放补记为已确认（参数 feed / tag / switch）
    LootFeedCollect  等列表停下，把还没读过的条目一次识别出来并播报

为什么不边拍边识别：识别一行要 0.15–0.4 秒（游戏同机运行时偏慢），而条目 0.2 秒来一条、
最快 1.6 秒就被挤出屏幕；截图只要几十毫秒。连拍期间不识别，读得全不全就与机器快慢无关。
跟踪与拼行的做法见 utils/loot_feed.py。

参数（custom_action_param，全部可省）：
    feed       会话名，同一任务内同名的动作共用一份跟踪状态。默认 "default"
    switch     Cast/Watch：一个节点名，它启用时才连拍，否则只点击（Watch 不点）并等 watch_ms（与原节点等价）
    tag        Cast：记在这次点击之后到达的条目上（如 "吸取"）。Watch：给了就打一个标记，之后到达、
               又没有哪次施放认领的条目记在它名下（如 "落地"）
    only       LootFeedCast：这次施放只会出的物品名（字典里的简体名）。之后到达的条目不在其中，就往前归给别的施放
    linger_ms  LootFeedCast：它是本轮最后一次施放、已被确认、之后却还没有任何条目来时，Collect 从确认起最多等这么久。
               压制的掉落实测在确认后 0.9–2.1 秒才到，偶尔更晚；默认 0 = 不额外等
    expect     LootFeedCast：true = 这次施放只要被游戏确认就一定出条目（吸取是，召集、压制不一定）。Collect 靠它判卡住
    watch_ms   Cast/Watch：至少连拍多久。默认 2000
    confirm    LootFeedCast：false 时不判「游戏确认」（见下）。默认 true
    after_ms   LootFeedCast：游戏确认之后至少再拍这么久。默认 1000
    max_ms     连拍或等待的上限。Cast/Watch 默认 5000，Collect 默认 4000
    grace_ms   Collect：最后一次施放被游戏确认后至少这么久才认为不会再来；没看到确认的从收取开始算。默认 1000
    settle_ms  Collect：列表这么久没有新动静才算停。默认 500
    label      Collect：写进结果与日志的标签
    refresh    Collect：一个节点名。施放已被游戏确认、列表却一条都没出时，用 run_task 跑它一次（见「卡住」）
    refresh_wait_ms  Collect：跑完 refresh 后最多等这么久看列表开不开始出。默认 2500

游戏确认：点下去到游戏真正放出技能隔多久不一定（实测 0.85–2.5 秒，探查生效时吸取的前摇更长），
列表在确认后 0.15–0.3 秒才开始出。LootFeedCast 连拍时拿每一帧重跑本节点自己的识别：按钮不再可点
（识别连续 0.3 秒不命中）就是确认了，从那一刻起再拍 after_ms。窗口内没等到确认就照常返回，
由流程回到分流节点重判——与原来「等 2 秒再判」一致。

版式（底框位置、格距…）读节点 Agt_LootFeed_Layout 的 attach，覆盖包改那个节点即可。

卡住：游戏偶尔在施放后不出列表（PC 与主城图实测），要进一次全屏页面再回到场景才放出来——开区域地图再退出、
点手图标进传送地图再退出都行，切技能页不行（用户 11-10 手测）。卡住是按施放分的：同一个点上吸取的列表卡住，
后面召集、压制的照常出（11-10 实跑），所以不能看「整个点有没有条目」。判据是：带 expect 的施放被游戏确认了，
却没有一条归到它名下（归属先按到达时刻，再按 only 纠正：吸取出得慢、拖到召集点下之后才来的材料仍算吸取的，
不会误判卡住）。命中时 Collect 跑一次 refresh 指的那段节点（开区域地图再点返回），接着连拍再读，
翻页后才到的条目记在卡住的那次施放名下；仍然没有就报警告、记 stuck，不再重试。
只在卡住的点多花这段时间。没配 refresh 时只报警告。

留痕：每次 Collect 往 debug/custom.log.loot_feed.jsonl.log 追加一行（条目、各条相对所属点击的到达时刻、
各次点击的确认与连拍时长）。GUI 控制台的日志不落盘，事后核对一次实跑靠这个文件；文件名以 custom.log
开头，MFAA 导出日志包时会一并收走。
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
TRAIL_NAME = "custom.log.loot_feed.jsonl.log"
TRAIL_MAX_BYTES = 2 * 1024 * 1024   # 留痕文件超过这么大就挪成 .prev 重开
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


def _append_trail(line):
    """留痕写不成只记 debug，不影响读取结果。"""
    try:
        directory = Path(__file__).resolve().parents[2] / "debug"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / TRAIL_NAME
        if target.exists() and target.stat().st_size > TRAIL_MAX_BYTES:
            target.replace(target.with_name(TRAIL_NAME.replace(".jsonl.log", ".prev.jsonl.log")))
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        mfaalog.debug(f"[LootFeed] 留痕文件写入失败: {e!r}")


def _owner(points, t_first, name):
    """这条条目归哪次施放（或哪个标记）：从它到达前最近的一个往回找，跳过声明了 only 而这样东西不在其中的。

    吸取的列表偶尔出得慢，拖到召集、压制点下之后才来；召集只出金币、压制只出金币和兽肉，材料不可能是它们的。
    名字没读出来（name 为 None）或往回都没人收，就按时间归最近的那个。
    """
    before = [p for p in points if p["t"] <= t_first]
    if name is not None:
        for p in reversed(before):
            if not p.get("only") or name in p["only"]:
                return p
    return before[-1] if before else None


def _late_owner(casts, starved, name):
    """翻页之后才到的条目归谁：只有某几次施放才出的东西（兽肉、金币）归其中最近的一次，其余归卡住的那次。

    翻页放出来的不一定只有卡住的那一批：11-10 实跑里压制的掉落来得晚，翻页回来时和吸取的材料一起出现。
    """
    if name is not None:
        for c in reversed(casts):
            if c.get("only") and name in c["only"]:
                return c
    return starved


def _collect(context, session, feed, params):
    tracker = session.tracker
    grace = float(params.get("grace_ms", 1000)) / 1000
    settle = float(params.get("settle_ms", 500)) / 1000
    max_s = float(params.get("max_ms", 4000)) / 1000
    waited = time.monotonic()
    # 流程走到出口时技能都已被确认；没在连拍里看到确认的，最晚就是此刻刚确认
    last = session.casts[-1] if session.casts else None
    since = None if last is None else (last["confirmed"] if last["confirmed"] is not None else waited)

    def quiet(now):
        latest = max(t for t in (tracker.t_last_new, tracker.t_last_glow, float("-inf")) if t is not None)
        return now - latest >= settle and tracker.unsettled(now) == 0

    # 最后一次施放要求多等（linger）：它已被确认、点下之后却还没有任何动静时，从确认起最多等到 linger
    linger = float(last.get("linger") or 0.0) if last is not None and last["confirmed"] is not None else 0.0

    def hold(now):
        if since is not None and now - since < grace:
            return True
        if linger and now - since < linger and max(tracker.t_last_new or 0.0, tracker.t_last_glow or 0.0) < last["t"]:
            return True
        return not quiet(now)

    _watch(context, session, 0.0, max(max_s, linger), hold)
    waited = time.monotonic() - waited

    points = sorted(session.casts + session.marks, key=lambda p: p["t"])
    got, owners, calls, read_s = {}, {}, 0, 0.0

    def read(batch, starved_cast=None):
        """识别一批条目并定归属；starved_cast 给定时这批是翻页之后才到的。"""
        nonlocal calls, read_s
        result, n, secs = _read(context, tracker, batch)
        calls, read_s = calls + n, read_s + secs
        for e in batch:
            name = known = None
            if e.idx in result:
                raw, count, score, attempt = result[e.idx]
                name, known = _resolve_name(raw, score)
                got[e.idx] = (raw, count, score, attempt, name, known)
            owners[e.idx] = (_late_owner(session.casts, starved_cast, name) if starved_cast is not None
                             else _owner(points, e.t_first, name))

    entries = [e for e in tracker.ordered() if not e.taken]
    read(entries)

    # 卡住：带 expect 的施放已被游戏确认，却没有一条归到它名下
    label = f"[{params['label']}]" if params.get("label") else ""
    starved = [c for c in session.casts if c.get("expect") and c["confirmed"] is not None
               and not any(owners[e.idx] is c for e in entries)]
    tags = "、".join(dict.fromkeys(str(c["tag"] or "?") for c in starved))
    refresh, late, rescue_s = params.get("refresh"), [], 0.0
    rescued = bool(starved and refresh and not context.tasker.stopping)
    if rescued:
        mfaalog.warning(f"[LootFeed]{label} {tags}已被游戏确认，但它的获得列表没出现（游戏偶发卡住），翻一次页面再读")
        rescue_s = time.monotonic()
        try:
            context.run_task(refresh)
        except Exception as e:
            mfaalog.error(f"[LootFeed]{label} 翻页节点 {refresh} 运行出错: {e!r}")
        back = time.monotonic()
        wait = float(params.get("refresh_wait_ms", 2500)) / 1000
        _watch(context, session, 0.0, wait + max_s,
               lambda now: (now - back < wait and (tracker.t_last_new or 0.0) < back) or not quiet(now))
        rescue_s = time.monotonic() - rescue_s
        late = [e for e in tracker.ordered() if not e.taken and e.idx not in owners]
        read(late, starved[0])        # 翻页之后才到的
        entries += late
    # 卡住的那次施放翻了页也没等到条目（或没配 refresh）：它的掉落没记到
    stuck = any(not any(owners[e.idx] is c for e in entries) for c in starved)

    items, unread = [], 0
    for e in entries:
        e.taken, e.crops = True, []
        if e.idx not in got:
            unread += 1
            continue
        raw, count, score, attempt, name, known = got[e.idx]
        owner = owners[e.idx]
        items.append({"name": name, "raw": raw, "count": count, "score": round(score, 4), "known": known,
                      "after": owner["tag"] if owner else None,
                      "at_ms": round((e.t_first - owner["t"]) * 1000) if owner else None,
                      "late": any(e is x for x in late), "mark": bool(owner and owner.get("mark")),
                      "reread": attempt, "frames": e.seen, "occluded": e.sig_changed})
    resync = sum(1 for n in tracker.notes if n[1] == "resync")
    tracker.notes.clear()
    record = {"time": time.time(), "feed": feed, "label": params.get("label"), "items": items,
              "unread": unread, "resync": resync}
    confirms = "、".join(
        f"{c['tag'] or '?'} " + ("未见确认" if c["confirmed"] is None else f"{(c['confirmed'] - c['t']) * 1000:.0f}ms")
        + f"/拍 {c.get('watched', 0) * 1000:.0f}ms" for c in session.casts)
    _append_trail({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "feed": feed, "label": params.get("label"),
        "items": [{k: i[k] for k in ("name", "raw", "count", "after", "at_ms", "late", "mark", "reread", "frames")}
                  for i in items],
        "unread": unread, "resync": resync, "starved": [c["tag"] for c in starved], "stuck": stuck, "rescued": rescued,
        "frames": session.frames, "wait_ms": round(waited * 1000), "rescue_ms": round(rescue_s * 1000),
        "ocr_calls": calls, "ocr_ms": round(read_s * 1000),
        "casts": [{"tag": c["tag"], "confirm_ms": None if c["confirmed"] is None else round((c["confirmed"] - c["t"]) * 1000),
                   "watched_ms": round(c.get("watched", 0) * 1000), "confirmed_late": bool(c.get("confirmed_late"))}
                  for c in session.casts]})
    session.casts.clear()
    session.marks.clear()
    loot_feed.add_record(feed, record)

    if items or unread:
        text = "、".join(f"{i['name']}×{i['count']}" for i in items) or "无"
        tail = f"；另有 {unread} 条没读成" if unread else ""
        unknown = [i["raw"] for i in items if not i["known"]]
        tail += f"；字典里没有: {'、'.join(unknown)}" if unknown else ""
        late_n = sum(1 for i in items if i["late"])
        tail += f"（{tags}卡住，其中 {late_n} 条是翻页后读出）" if late_n else ""
        marked = [i for i in items if i["mark"]]
        tail += f"（其中 {len(marked)} 条不是技能放出来的，{marked[0]['after']}时就有）" if marked else ""
        mfaalog.info(f"[LootFeed]{label} 掉落 {len(items)} 条: {text}{tail}")
    if stuck:
        again = "翻过一次页面仍" if rescued else "一直"
        mfaalog.warning(f"[LootFeed]{label} {tags}已被游戏确认，但它的获得列表{again}没出现（游戏偶发卡住），这部分掉落未记录")
    mfaalog.debug(f"[LootFeed]{label} 连拍累计 {session.frames} 帧，收尾等待 {waited * 1000:.0f}ms，"
                  f"翻页补读 {rescue_s * 1000:.0f}ms，识别 {calls} 次 {read_s * 1000:.0f}ms，重同步 {resync} 次；"
                  f"各次点击到确认/连拍时长: {confirms or '无'}")
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

        cast = {"t": time.monotonic(), "tag": params.get("tag"), "confirmed": None,
                "expect": bool(params.get("expect", False)), "only": list(params.get("only") or []),
                "linger": float(params.get("linger_ms", 0)) / 1000}
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
            if cast["confirmed"] is None:
                return miss_since is not None      # 按钮刚开始不中：等这一段判完再走（11-10 实跑差 15ms 没记上确认）
            return now - cast["confirmed"] < after_s

        _watch_safely(context, session, watch_s, float(params.get("max_ms", 5000)) / 1000, hold, on_frame)
        cast["watched"] = time.monotonic() - cast["t"]
        return bool(clicked)


@AgentServer.custom_action("LootFeedConfirm")
class LootFeedConfirmAction(CustomAction):
    """流程自己认出「这个技能已经用掉了」时调用：把同 tag 的最近一次施放补记为已确认。

    游戏吃下技能偶尔比连拍窗口还晚（实测点击后最长 2.5 秒），那一次施放在连拍里就没等到确认；
    流程随后在分流节点认出按钮已用，从这里告诉读取组件。没有对应的施放、或它已经确认过，什么都不做。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _params(argv)
            if not _switched_on(context, params):
                return True
            session = loot_feed.peek_session(_task_id(argv), str(params.get("feed", "default")))
            tag = params.get("tag")
            cast = next((c for c in reversed(session.casts if session else []) if c["tag"] == tag), None)
            if cast is not None and cast["confirmed"] is None:
                cast["confirmed"] = time.monotonic()
                cast["confirmed_late"] = True
        except Exception as e:
            mfaalog.error(f"[LootFeed] LootFeedConfirm 出错: {e!r}")
        return True


@AgentServer.custom_action("LootFeedWatch")
class LootFeedWatchAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        watch_s = 2.0
        try:
            params = _params(argv)
            watch_s = float(params.get("watch_ms", 2000)) / 1000
            if not _switched_on(context, params):
                time.sleep(watch_s)       # 开关没开：与原节点的固定等待等价
                return True
            session = _session(context, argv, str(params.get("feed", "default")))
        except Exception as e:
            mfaalog.error(f"[LootFeed] LootFeedWatch 参数或会话出错，退回固定等待: {e!r}")
            time.sleep(watch_s)
            return True
        if params.get("tag"):
            if not session.casts and all(e.taken for e in session.tracker.entries):
                session.marks.clear()     # 上一个点什么都没发生、没走到收取：它的标记不带到这个点
            session.marks.append({"t": time.monotonic(), "tag": params["tag"], "mark": True})
        _watch_safely(context, session, watch_s, max(watch_s, float(params.get("max_ms", 5000)) / 1000),
                      lambda now: session.tracker.unsettled(now) > 0)
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
