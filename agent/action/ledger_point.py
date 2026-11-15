"""库存记账的位置动作：流程走到一个位置，留标记，按规则决定盘不盘。

节点在 Ledger.json，参数只说明这是哪里：
    {"mark": "begin" | "end" | "queue_end" | "manual", "type": "daily" | "collect" | "shop"}
begin 是任务开始动库存的地方，end 是只有做成才走得到的正常结束点；queue_end、manual 不带类型。
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
from utils.inventory_ledger import CARD_PREFIXES
from utils.inventory_store import get_events, save_currency_reading, save_ledger_mark, utc_now
from utils.persistent_store import PersistentStore

from .bag_scan import run_bag_scan, scope_names
from .currency_read import read_currency


CONFIG = "Agt_Ledger_Config"
QUIET = ledger_rules.QUIET   # 没留标记也不算活动的任务入口；Agt_Ledger_Config.attach.quiet 读不到时用这份
KIND_NAMES = {"daily": "日常", "collect": "吸取", "shop": "商店"}
# 只有存货（商店套利的食材与料理）没盘全才退出背包、再进来重扫一次（U61）；票券、养成、装备材料没盘全照旧跨过去。
RETRY_SCOPE = "A"
# 重扫也好不了的：物品表里没有它、任务被停了。
_NO_RETRY = frozenset({"template_missing", "stopped"})

_LOCK = threading.Lock()
_STATES: dict = {}    # 账号 → LedgerState。键是账号编号，不是对象地址。
_CURSORS: dict = {}   # 账号 → 上一条标记的时刻（秒）


def _label(mark: str, kind) -> str:
    if mark == "begin":
        return f"记账·{KIND_NAMES[kind]}开头"
    if mark == "end":
        return f"记账·{KIND_NAMES[kind]}结束"
    return "记账·结束游戏" if mark == "queue_end" else "记账·手动结账"


def _iso(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def _settings(context) -> dict:
    try:
        attach = dict(context.get_node_object(CONFIG).attach or {})
    except Exception:
        attach = {}
    quiet = attach.get("quiet")
    gap = attach.get("gap_seconds")
    return {"quiet": tuple(quiet) if isinstance(quiet, list) and quiet else QUIET,
            "gap": float(gap) if isinstance(gap, (int, float)) and gap > 0 else ledger_rules.GAP_SECONDS}


def _shown(value) -> str:
    return "?" if value is None else f"{value:,}"


def _read_currency(context, label: str, scan_id: str) -> None:
    """盘点返回后画面在主页或箱庭，顶栏有货币；读不到的项记空，读数异常只记日志。"""
    try:
        record = read_currency(context)
        record.update(label=label, scan_id=scan_id)
        saved = save_currency_reading(record)
        mfaalog.info(f"[Ledger] {label}：钻石 {_shown(record['diamond'])}、金币 {_shown(record['gold'])}、"
                     f"天赋神药 {_shown(record['tonic'])}" + ("" if saved else "（⚠️ 存档未写入）")
                     + (f"（钻石的免费 / 付费没读成：{record['detail_error']}）" if record.get("detail_error") else ""))
    except Exception as exc:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：货币没读成：{exc}")


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


def _retry_stock(context, label: str, record: dict, origin: dict) -> bool:
    """存货有没盘准的：退出背包（盘点本来就原路返回了）再进来，只找那几样重扫一次，记录带 retry_of 指回原来那次；
    做账时补上的读数并进原来那次盘点。只重扫一次，还不全就照旧跨过去。返回最后一趟有没有回到原位（决定读不读货币）。"""
    returned = bool(record.get("returned"))
    if RETRY_SCOPE not in (record.get("scope") or ()):
        return returned
    stock = scope_names(context, RETRY_SCOPE)
    missing = [name for name, reason in (record.get("unknown") or {}).items() if name in stock and reason not in _NO_RETRY]
    if not missing:
        return returned
    mfaalog.info(f"[Ledger] {label}：存货有 {len(missing)} 项没盘准，退出背包再进来重扫一次")
    again = run_bag_scan(context, label=f"{label}·重扫", targets=missing, scope=[RETRY_SCOPE],
                         origin={**origin, "retry_of": record["scan_id"]})
    if not again.get("executed"):
        mfaalog.warning(f"[Ledger] ⚠️ {label}：重扫没执行（{again.get('error')}），没盘准的跨过去")
        return returned
    left = [name for name in missing if name in (again.get("unknown") or {})]
    if left:
        mfaalog.warning(f"[Ledger] ⚠️ {label}：重扫后仍有 {len(left)} 项没盘准，跨过去")
    else:
        mfaalog.info(f"[Ledger] {label}：重扫补齐了 {len(missing)} 项")
    return bool(again.get("returned"))


def _open(path) -> None:
    """用系统默认浏览器打开报表。只在手动结账后调：那时人就在电脑前，别处弹窗会抢游戏的前台。"""
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
        result = inventory_report_output.generate_reports(
            folder, PersistentStore._current_account_id, agent_dir=Path(__file__).resolve().parents[1], hot=get_events(),
            entry_dir=root)
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
    decision = ledger_rules.decide(mark, kind, state, task_id=task_id, rows=rows, now=now,
                                   quiet=settings["quiet"], gap=settings["gap"])
    if decision["scan"] and options["full"]:
        decision["scopes"] = list(ledger_rules.ALL_SCOPES)   # 选项「每次全盘」：盘不盘照规则，范围一律全盘

    task = {"entry": entry, "task_id": task_id}
    if mark != "end" or decision["end_mark"]:
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

    scan = None
    if decision["scan"]:
        mfaalog.info(f"[Ledger] {label}：盘点（{'+'.join(decision['scopes'])}）——{decision['reason']}")
        origin = {"mark": mark, "type": kind, "node": argv.node_name, "task": task, "session": task_journal.SESSION}
        record = run_bag_scan(context, label=label, scope=decision["scopes"], origin=origin)
        if not record.get("executed"):
            mfaalog.warning(f"[Ledger] ⚠️ {label}：盘点没执行（{record.get('error')}），留给下一个位置")
        else:
            scan = {"scan_id": record["scan_id"], "scopes": record.get("scope") or decision["scopes"]}
            record["returned"] = _retry_stock(context, label, record, origin)
            if record.get("returned"):
                _read_currency(context, label, record["scan_id"])
            else:
                mfaalog.warning(f"[Ledger] ⚠️ {label}：盘点后没能回到原位，货币没读，页面由所在流程自行恢复")
    else:
        mfaalog.debug(f"[Ledger] {label}：不盘——{decision['reason']}")
    with _LOCK:
        ledger_rules.settle(state, mark=mark, kind=kind, task_id=task_id, entry=entry, now=time.time(), scan=scan)
    # 盘了就出报表；结束游戏与手动结账即使规则说不用盘也出一次：收个尾，把最后几个任务的起止补进去。
    # 只有手动结账出完自己打开（U61）。
    if options["report"] and (scan is not None or mark in ("queue_end", "manual")):
        _report(label, show=mark == "manual")


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
