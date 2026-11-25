"""库存记账的位置动作：流程走到一个位置，留标记，按规则决定盘不盘。

节点在 Ledger.json，参数只说明这是哪里：
    {"mark": "begin" | "end" | "queue_end" | "manual" | "probe", "type": "daily" | "collect" | "shop" | "exchange"}
begin 是任务开始动库存的地方，end 是只有做成才走得到的正常结束点；queue_end、manual 不带类型。
商店兑换（代币商店）有两处：进店前的 probe 只判要不要补盘、不留标记；离店后真买成过才走到 begin（事后留的始标记，
不在那里盘）。两处都另读一次顶栏货币，做账时拿它们把同一段里兑换与套利动的金币、天赋神药分开。
盘不盘的规则在 utils/ledger_rules.py。这里只留凭证（标记、盘点读数、货币读数）；
段怎么划、算谁的，是出报表时从凭证现算的事。
永远返回 True：记账出什么错都不打断所在的业务。
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import inventory_report_output, ledger_config, ledger_rules, loot_cards, mfaalog, task_journal
from utils.account_sync import current_session, sync_from_context
from utils.bag_scan_engine import FINISHED
from utils.inventory_ledger import CARD_PREFIXES
from utils.inventory_store import get_events, save_currency_reading, save_ledger_mark, utc_now
from utils.persistent_store import PersistentStore

from .bag_scan import run_bag_scan, scope_names
from .currency_read import read_currency


CONFIG = "Agt_Ledger_Config"   # 免记名单与连续阈值所在的节点，见 _settings
KIND_NAMES = {"daily": "日常", "collect": "吸取", "shop": "商店", "exchange": "商店兑换"}
# 只有存货（商店套利的食材与料理）没盘全才退出背包、再进来重扫一次（U61）；票券、养成、装备材料没盘全照旧跨过去。
RETRY_SCOPE = "A"
# 重扫也好不了的：物品表里没有它、任务被停了。
_NO_RETRY = frozenset({"template_missing", "stopped"})
# 顶栏三大货币：读数里的字段 → 日志里的名字。
CURRENCY = (("gold", "金币"), ("diamond", "钻石"), ("tonic", "天赋神药"))
CURRENCY_TRIES = 3     # 每个地方读几次
CURRENCY_WAIT = 1.0    # 同一个地方两次之间隔几秒
# 收尾位置原地读不全时依次去这些地方再读（复位模块，关弹窗、返回、等加载都由 pipeline 负责）。商店收尾本来就停在箱庭。
CURRENCY_PLACES = (("Global_ToSandBox", "回箱庭"), ("Global_ToHomePage", "回主界面"))

_LOCK = threading.Lock()
_STATES: dict = {}    # 账号 → LedgerState。键是账号编号，不是对象地址。
_CURSORS: dict = {}   # 账号 → 上一条标记的时刻（秒）
_SETTINGS: dict = {}  # 本进程从节点 Agt_Ledger_Config 读成过的名单与阈值；空着表示还没读成过


def _label(mark: str, kind) -> str:
    if mark == "probe":
        return f"记账·{KIND_NAMES[kind]}开头"
    if mark == "begin" and kind in ledger_rules.LATE:
        return f"记账·{KIND_NAMES[kind]}收尾"   # 事后留的始标记：位置在离店之后
    if mark == "begin":
        return f"记账·{KIND_NAMES[kind]}开头"
    if mark == "end":
        return f"记账·{KIND_NAMES[kind]}结束"
    return "记账·结束游戏" if mark == "queue_end" else "记账·手动结账"


def _iso(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def _held_settings() -> dict:
    """本进程留着的名单与阈值；还没从节点读成过就是内置值。出报表手上没有 context，用的就是这份。"""
    with _LOCK:
        return dict(_SETTINGS) if _SETTINGS else ledger_rules.settings(None)


def _settings(context) -> dict:
    """免记名单与连续阈值，以节点 Agt_Ledger_Config.attach 为准（资源包、选项对它的覆盖都落在节点上）。

    读成一次就留在进程里：盘不盘的判定和之后出的报表用同一份；哪次节点读不到就沿用上一次读成的，
    本进程从没读成过才用 ledger_rules 里的内置值。
    """
    try:
        attach = context.get_node_object(CONFIG).attach
    except Exception:
        attach = None
    if isinstance(attach, dict) and attach:
        fresh = ledger_rules.settings(attach)
        with _LOCK:
            _SETTINGS.clear()
            _SETTINGS.update(fresh)
    return _held_settings()


def _shown(value) -> str:
    return "?" if value is None else f"{value:,}"


def _missing(record) -> list:
    return [name for field, name in CURRENCY if record is None or record.get(field) is None]


def _merge_currency(record, fresh) -> dict:
    """几次读数拼成一条：先读到的项不换；钻石连同免费 / 付费一起取第一次读到总数的那次。几秒之内这几样不会变。"""
    if record is None:
        return fresh
    merged = dict(record, observed_at=fresh["observed_at"],
                  icons_found=sorted(set(record.get("icons_found") or ()) | set(fresh.get("icons_found") or ())))
    for field in ("gold", "tonic"):
        if merged.get(field) is None:
            merged[field] = fresh.get(field)
    if merged.get("diamond") is None and fresh.get("diamond") is not None:
        merged.update({key: fresh.get(key) for key in ("diamond", "diamond_free", "diamond_paid", "diamond_paid_source",
                                                      "detail_error", "detail_closed")})
    return merged


def _read_currency(context, label: str, scan_id: str, *, roam: bool, here: bool = True) -> None:
    """盘点之后读顶栏三大货币，挂在这次盘点名下。读不全就原地隔一秒再读，最多 CURRENCY_TRIES 次。
    roam（收尾位置）：还读不全就回箱庭、再回主界面，各读几次（U68）。收尾之后没有下一次盘点，玩家接着手动玩会改库存，
    这一盘是下一日期初调整的基准，要收干净。收尾位置都是任务的最后一步，换页面不打乱所在流程；任务开头的位置不换。
    here=False：盘点没回到原位，页面不明，不在原地读。读不到的项记空，读数异常只记日志。"""
    record, tried = None, []
    for place, where in (((None, "原地"),) if here else ()) + (CURRENCY_PLACES if roam else ()):
        if place is not None:
            try:
                if context.tasker.stopping:
                    break
                mfaalog.info(f"[Ledger] {label}：货币还缺{'、'.join(_missing(record))}，{where}再读")
                detail = context.clone().run_task(place)
                if detail is None or not detail.status.succeeded:
                    mfaalog.warning(f"[Ledger] ⚠️ {label}：{where}没成功，照样试读")
            except Exception as exc:
                mfaalog.warning(f"[Ledger] ⚠️ {label}：{where}出错（{exc}），照样试读")
        for attempt in range(CURRENCY_TRIES):
            if attempt:
                time.sleep(CURRENCY_WAIT)
            try:
                fresh = read_currency(context, diamond_detail=None if record is None or record.get("diamond") is None else False)
            except Exception as exc:
                mfaalog.warning(f"[Ledger] ⚠️ {label}：货币没读成：{exc}")
                continue
            record = _merge_currency(record, fresh)
            if not _missing(record):
                break
        tried.append(where)
        if not _missing(record):
            break
    if record is None:
        return
    try:
        record.update(label=label, scan_id=scan_id)
        saved = save_currency_reading(record)
        missing = _missing(record)
        (mfaalog.warning if missing else mfaalog.info)(
            f"[Ledger] {'⚠️ ' if missing else ''}{label}：钻石 {_shown(record['diamond'])}、金币 {_shown(record['gold'])}、"
            f"天赋神药 {_shown(record['tonic'])}" + ("" if saved else "（⚠️ 存档未写入）")
            + (f"（{'、'.join(tried)}各读过，还缺{'、'.join(missing)}）" if missing else "")
            + (f"（钻石的免费 / 付费没读成：{record['detail_error']}）" if record.get("detail_error") else ""))
    except Exception as exc:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：货币读数没存成：{exc}")


# 商店兑换前后各读一次的顶栏：做账时用这两次的差把同一段里兑换与套利各自动的拆开（代币商店接入计划 §2.2）。
BAR_NEEDED = (("gold", "金币"), ("tonic", "天赋神药"))


def _read_bar(context, label: str, origin: dict) -> None:
    """不跟盘点的一次顶栏读数（商店兑换的进店前、离店后），带 origin 指回这个位置，不挂在任何盘点名下。
    只要金币与天赋神药：缺了原地隔一秒再读，最多 CURRENCY_TRIES 次；不点开钻石弹窗、不换页面（代币商店的开头与收尾
    都先回了主页）。读不到的项记空，做账时那一样就不拆、整笔留在套利那一列。出错只记日志。"""
    record = None
    for attempt in range(CURRENCY_TRIES):
        if attempt:
            time.sleep(CURRENCY_WAIT)
        try:
            record = _merge_currency(record, read_currency(context, diamond_detail=False))
        except Exception as exc:
            mfaalog.warning(f"[Ledger] ⚠️ {label}：顶栏没读成：{exc}")
            continue
        if all(record.get(field) is not None for field, _ in BAR_NEEDED):
            break
    if record is None:
        return
    try:
        record.update(label=label, scan_id=None, origin=origin)
        saved = save_currency_reading(record)
        missing = [name for field, name in BAR_NEEDED if record.get(field) is None]
        (mfaalog.warning if missing else mfaalog.info)(
            f"[Ledger] {'⚠️ ' if missing else ''}{label}：顶栏金币 {_shown(record['gold'])}、天赋神药 {_shown(record['tonic'])}"
            + ("" if saved else "（⚠️ 存档未写入）") + (f"（还缺{'、'.join(missing)}）" if missing else ""))
    except Exception as exc:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：顶栏读数没存成：{exc}")


def _cycle_marks():
    """账号存档里采集卡带的周期完成记录 {"卡带名@周期": 完成时刻}。吸取的始末标记各带一份，做账时取前后差就是这一段吸完的卡。"""
    try:
        marks = PersistentStore.load().get("cycles")
        return {str(key): str(value) for key, value in marks.items()
                if str(key).startswith(CARD_PREFIXES)} if isinstance(marks, dict) else {}
    except Exception:
        return None


def _hand_over_loot(mark: str, kind, task_id, account) -> None:
    """吸取掉落按卡带落盘：吸取结束位置替最后一张卡交，别的位置交上一个任务中途停下没交的。只记日志，不拦记账。"""
    try:
        text = loot_cards.store(loot_cards.settle(task_id, task_end=mark == "end" and kind == "collect"), account)
        if text:
            (mfaalog.warning if text.startswith("⚠️") else mfaalog.info)(f"[Ledger] {text}")
    except Exception as exc:
        mfaalog.warning(f"[Ledger] ⚠️ 吸取掉落没能落盘（不影响记账）：{exc}")


def _lost(record: dict) -> set:
    """半路断了的盘点里，因为断了而没走到的物品（记成未知、原因是结束原因，或角标收下还没定的）；正常走完的为空。"""
    termination = record.get("termination")
    if termination in FINISHED:
        return set()
    return {name for name, reason in (record.get("unknown") or {}).items() if reason in (termination, "corner_unconfirmed")}


def _retry(context, label: str, record: dict, origin: dict, *, closing: bool) -> tuple[bool, set]:
    """没盘准的退出背包（盘点本来就原路返回了）再进来，只找那几样重扫一次，记录带 retry_of 指回原来那次；
    做账时补上的读数并进原来那次盘点。只重扫一次，还不全就照旧跨过去。
    平时只重扫存货（U61）；收尾位置的盘点半路断了，没走到的各类一起重扫（U69）——收尾之后没有下一盘。任务被停的不重扫。
    返回（最后一趟有没有回到原位——决定读不读货币，重扫补上了哪几样）。"""
    returned = bool(record.get("returned"))
    scope, unknown = list(record.get("scope") or ()), record.get("unknown") or {}
    stock = scope_names(context, RETRY_SCOPE) if RETRY_SCOPE in scope else set()
    missing = [name for name, reason in unknown.items() if name in stock and reason not in _NO_RETRY]
    scopes, why = [RETRY_SCOPE], f"存货有 {len(missing)} 项没盘准"
    lost = _lost(record) if closing and record.get("termination") != "stopped" else set()
    extra = [name for name in unknown if name in lost and name not in missing]
    if extra:
        missing += extra
        scopes = scope
        why = f"盘点半路断了（{record.get('termination')}），收尾之后没有下一盘：没走到的 {len(missing)} 项"
    if not missing:
        return returned, set()
    mfaalog.info(f"[Ledger] {label}：{why}，退出背包再进来重扫一次")
    again = run_bag_scan(context, label=f"{label}·重扫", targets=missing, scope=scopes,
                         origin={**origin, "retry_of": record["scan_id"]})
    if not again.get("executed"):
        # 没走到扫描核心也可能已经离开原位（进包半路失败）：位置看这一趟，不沿用首趟的。
        mfaalog.warning(f"[Ledger] ⚠️ {label}：重扫没执行（{again.get('error')}），没盘准的跨过去")
        return bool(again.get("returned")), set()
    left = [name for name in missing if name in (again.get("unknown") or {})]
    if left:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：重扫后仍有 {len(left)} 项没盘准，跨过去")
    else:
        mfaalog.info(f"[Ledger] {label}：重扫补齐了 {len(missing)} 项")
    return bool(again.get("returned")), set(missing) - set(left)


def _finished(record: dict, filled: set) -> bool:
    """这次盘点算不算盘过（记账规则 §7）。正常走完的算；半路断了的不算，未盘掉的活动留给下一个位置。
    不能看 complete：有一样没读准它就是假，那种照算盘过，账逐物品跨过去。
    断掉没走到的全被重扫补上了，也算盘过。"""
    if record.get("termination") in FINISHED:
        return True
    lost = _lost(record)
    return bool(lost) and lost <= filled


def _resume(state, label: str, *, task_id: int, now: float, gap: float) -> None:
    """本进程还没盘过：上一次运行刚在收尾位置盘完（不超过连续阈值）就接着它，当作本任务开头刚盘过。
    条件见 ledger_rules.resume_from；读凭证出错就按没接上处理，照常全盘。"""
    try:
        resumed = ledger_rules.resume_from(get_events(), now=now, gap=gap, finished=FINISHED)
    except Exception as exc:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：上一次运行的盘点没读成（{exc}），照常盘")
        return
    if resumed is None:
        return
    with _LOCK:
        if state.last_scan is None:
            state.last_scan = {"scan_id": resumed["scan_id"], "at": resumed["at"], "scopes": resumed["scopes"],
                               "game_day": resumed["game_day"], "task_id": task_id}
    mfaalog.info(f"[Ledger] {label}：接着上一次运行的「{resumed['label']}」（{round(resumed['age'])} 秒前盘完）")


def _open(path) -> None:
    """用系统默认浏览器打开报表。手动结账后与商店收尾盘后调（U61、11-19 用户定：跑完出个结果）。"""
    try:
        import webbrowser
        if not webbrowser.open(Path(path).resolve().as_uri()):
            mfaalog.info(f"[Ledger] 没能自动打开报表，请手动打开：{path}")
    except Exception as exc:
        mfaalog.info(f"[Ledger] 没能自动打开报表（{exc}），请手动打开：{path}")


def _report(label: str, *, show: bool = False) -> None:
    """盘完出报表：从凭证现算，重写还没结账的游戏日。show：出完用浏览器打开（桌面才开）。
    出什么错都只记日志，不影响所在流程。"""
    try:
        folder = getattr(PersistentStore, "CONFIG_DIR", None)
        if folder is None:
            return
        started = time.perf_counter()
        # 入口页放安装目录（桌面才有；安卓与宿主指定存档目录时没有这个位置）。
        root = getattr(getattr(PersistentStore, "_storage_policy", None), "portable_root", None)
        held = _held_settings()   # 做账划段用的名单与阈值，和运行时判盘不盘的是同一份
        result = inventory_report_output.generate_reports(
            folder, PersistentStore._current_account_id, agent_dir=Path(__file__).resolve().parents[1], hot=get_events(),
            quiet=held["quiet"], gap=held["gap"], entry_dir=root)
        if result["summary"]:
            mfaalog.info(f"[Ledger] 库存日报：{result['summary']}")
            where = f"安装目录的「{result['entry'].name}」可直接打开；原件在 {result['latest']}" if result.get("entry") \
                else f"报表在 {result['latest']}"
            mfaalog.info(f"[Ledger] {where}（{round((time.perf_counter() - started) * 1000)} 毫秒）")
        if show and root is not None and result["latest"]:
            _open(result["latest"])
    except Exception as exc:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：报表没出成（不影响任务）：{exc}")


def _run(context: Context, argv: CustomAction.RunArg) -> None:
    raw = argv.custom_action_param
    params = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
    mark, kind = str(params.get("mark") or ""), params.get("type")
    if mark not in ledger_rules.MARKS:
        raise ValueError(f"参数 mark 不对：{mark!r}")
    options = ledger_config.ledger_options(current_session())
    if not options["scan"]:
        return
    # stop=False：账号未确认时只是不记账，不替所在流程停任务。
    if not sync_from_context(context, where="LedgerPoint", stop=False):
        mfaalog.warning("[Ledger] ⚠️ 账号未确认，这个位置不留标记、不盘")
        return
    account = PersistentStore._current_account_id
    task_id = argv.task_detail.task_id
    _hand_over_loot(mark, kind, task_id, account)   # 先于标记与盘点：这次出的报表就用得上
    rows = task_journal.rows()
    entry = getattr(argv.task_detail, "entry", None) or next(
        (row["entry"] for row in rows if row["task_id"] == task_id), "")
    settings = _settings(context)
    label, now = _label(mark, kind), time.time()
    with _LOCK:
        state = _STATES.setdefault(account, ledger_rules.LedgerState())
        since = _CURSORS.get(account, 0.0)
        fresh = state.last_scan is None
    if fresh:
        _resume(state, label, task_id=task_id, now=now, gap=settings["gap"])
    decision = ledger_rules.decide(mark, kind, state, task_id=task_id, rows=rows, now=now,
                                   quiet=settings["quiet"], gap=settings["gap"])
    if decision["scan"] and options["full"]:
        decision["scopes"] = list(ledger_rules.ALL_SCOPES)   # 选项「每次全盘」：盘不盘照规则，范围一律全盘

    task = {"entry": entry, "task_id": task_id}
    if mark != "probe" and (mark != "end" or decision["end_mark"]):   # probe 只判盘不盘，不留标记
        saved = save_ledger_mark({
            "mark": mark, "type": kind, "at": utc_now(), "session": task_journal.SESSION, "task": task,
            "node": argv.node_name, **({"cards": _cycle_marks()} if kind == "collect" else {}),
            "tasks": [{"entry": row["entry"], "task_id": row["task_id"], "start": _iso(row["start"]),
                       "end": _iso(row["end"]), "status": row["status"]}
                      for row in rows if row["end"] is None or row["end"] >= since]})   # 上一条标记之后还在跑过的
        if not saved:
            mfaalog.warning(f"[Ledger] ⚠️ {label}：标记没写进存档")
        with _LOCK:
            _CURSORS[account] = now

    scan, executed = None, False
    origin = {"mark": mark, "type": kind, "node": argv.node_name, "task": task, "session": task_journal.SESSION}
    try:
        if decision["scan"]:
            mfaalog.info(f"[Ledger] {label}：盘点（{'+'.join(decision['scopes'])}）——{decision['reason']}")
            record = run_bag_scan(context, label=label, scope=decision["scopes"], origin=origin)
            executed = bool(record.get("executed"))
            if not executed:
                mfaalog.warning(f"[Ledger] ⚠️ {label}：盘点没执行（{record.get('error')}），留给下一个位置；"
                                "这个任务里出售与补查不认更早的库存读数")
            else:
                closing = mark in ledger_rules.CLOSING   # 收尾位置：任务的最后一步，后面没有下一盘，可以换页面去把货币读到
                record["returned"], filled = _retry(context, label, record, origin, closing=closing)
                if record.get("returned") or closing:
                    if not record.get("returned"):
                        mfaalog.warning(f"[Ledger] ⚠️ {label}：盘点后没能回到原位，去箱庭读货币")
                    _read_currency(context, label, record["scan_id"], roam=closing, here=bool(record.get("returned")))
                else:
                    mfaalog.warning(f"[Ledger] ⚠️ {label}：盘点后没能回到原位，货币没读，页面由所在流程自行恢复")
                # 读到的数照样留在凭证里、做账照用；断了的只是不当作盘过，下一个位置还能补。
                if _finished(record, filled):
                    scan = {"scan_id": record["scan_id"], "scopes": record["scope"]}
                else:
                    mfaalog.warning(f"[Ledger] ⚠️ {label}：盘点半路断了（{record.get('termination')}），不算盘过，留给下一个位置")
        else:
            mfaalog.debug(f"[Ledger] {label}：不盘——{decision['reason']}")
    finally:
        # 标记这时已经落盘，盘点这一段不管出什么错，内存里的状态都要跟上（穿透复核第 7 条）：
        # 不跟上的话，同一任务走到结束位置时认不出自己留过始标记——结束标记不留，这一对标记在账上成了
        # 「未正常结束」，这一段的活动也没人收尾盘。出错的那次按没盘算，未盘掉的活动原样留给下一个位置。
        with _LOCK:
            ledger_rules.settle(state, mark=mark, kind=kind, task_id=task_id, entry=entry, now=time.time(), scan=scan)
        # 该盘而没执行＝最近那次盘点之后的活动还没盘掉：记在任务行上，出售与补查看到就不认更早的读数
        # （utils/arbitrage_sale_skip.trust_since，穿透复核第 4 条）。半路断了的留有盘点记录，读到的那部分照认。
        task_journal.note(task_id, stock_current=not decision["scan"] or executed)
    if mark == "probe" or (mark == "begin" and kind in ledger_rules.LATE):
        _read_bar(context, label, origin)   # 补盘了也另读一次：盘点那次挂在盘点名下，这一次挂在这个位置名下
    # 盘了就出报表（半路断了的也出：读到的那部分已经进了凭证）；结束游戏与手动结账即使规则说不用盘也出一次：
    # 收个尾，把最后几个任务的起止补进去。手动结账（U61）与商店收尾盘（11-19 用户定：商店一般排在最后，跑完出个结果）
    # 出完自己打开。
    if options["report"] and (executed or mark in ("queue_end", "manual")):
        _report(label, show=mark == "manual" or (mark == "end" and kind == "shop" and executed))


@AgentServer.custom_action("LedgerConfig")
class LedgerConfig(CustomAction):
    """任务「⚙️ 库存记账」的动作：非 MFAA 客户端在这里把本任务下发的选项记进内存，并报出当前生效的设置。

    MFAA 不靠它——那边每个位置直接读本实例配置，这个任务勾不勾、跑没跑都一样。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            session = current_session()
            if getattr(session, "source", None) != "mfaa":
                node = context.get_node_object(ledger_config.INJECT_NODE)
                ledger_config.remember_injected(getattr(node, "attach", None))
            options = ledger_config.ledger_options(session)
            mfaalog.info(f"[Ledger] 库存记账：{'开' if options['scan'] else '关'}；"
                         f"盘点范围：{'每次全盘' if options['full'] else '按规则'}；"
                         f"报表：{'出' if options['report'] else '不出'}")
        except Exception as exc:
            mfaalog.error(f"[Ledger] ❌ 记账设置读取异常，按默认处理：{exc}")
        return True


@AgentServer.custom_action("LedgerPoint")
class LedgerPoint(CustomAction):
    """库存记账的位置：留标记，按「上一次盘点之后有没有还没盘掉的活动」决定盘不盘。参数 {mark, type?}。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            _run(context, argv)
        except Exception as exc:
            mfaalog.error(f"[Ledger] ❌ 记账位置处理异常（不影响所在流程）：{exc}")
        return True
