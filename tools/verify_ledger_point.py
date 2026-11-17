"""库存记账位置动作回归：留什么标记、盘点带什么触发位置、货币读数挂哪次盘点，以及出错时不打断流程。

盘点、货币读数、账号同步都用替身，存档写内存，不连游戏。规则本身的用例在 verify_ledger_rules.py。
需要 maa 包（3.11 venv）。导入 action 包时把各注册装饰器换成原样放行，本机没有 Agent 客户端也能跑。直接运行：
    python -B tools/verify_ledger_point.py -v
"""

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from maa.library import Library  # noqa: E402

Library.version()  # 在导入 maa.agent 前固定为本地 MaaFramework 模式。
from maa.agent.agent_server import AgentServer  # noqa: E402


def _keep(*args, **kwargs):
    return lambda cls: cls


with patch.object(AgentServer, "custom_action", _keep), patch.object(AgentServer, "custom_recognition", _keep), \
        patch.object(AgentServer, "tasker_sink", _keep), patch.object(AgentServer, "context_sink", _keep), \
        patch.object(AgentServer, "_set_api_properties", lambda *args, **kwargs: None):
    from action import ledger_point as point  # noqa: E402
from utils import arbitrage_store as store  # noqa: E402
from utils import ledger_config, task_journal  # noqa: E402
from utils.ledger_rules import ALL_SCOPES  # noqa: E402

T0 = 1_763_208_000.0   # 2025-11-15 12:00 UTC
REAL_REPORT = point._report


class MemoryAccountStore:
    data = {}
    _current_account_id = "7"

    @classmethod
    def load(cls):
        return copy.deepcopy(cls.data)

    @classmethod
    def save(cls, data):
        cls.data = copy.deepcopy(data)
        return True


class NoConfig:
    def get_node_object(self, name):
        raise KeyError(name)


class Roaming(NoConfig):
    """收尾位置读货币时会换页面：记下跑了哪些复位任务。"""

    def __init__(self, stopping=False):
        self.tasker = SimpleNamespace(stopping=stopping)
        self.ran = []

    def clone(self):
        return self

    def run_task(self, entry, override=None):
        self.ran.append(entry)
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))


def argv(mark, kind, task_id, entry, node="Ledger_Node"):
    param = {"mark": mark} if kind is None else {"mark": mark, "type": kind}
    return SimpleNamespace(custom_action_param=json.dumps(param, ensure_ascii=False), node_name=node,
                           task_detail=SimpleNamespace(task_id=task_id, entry=entry))


class LedgerPointTests(unittest.TestCase):
    def setUp(self):
        MemoryAccountStore.data = {}
        task_journal.reset()
        point._STATES.clear()
        point._CURSORS.clear()
        point.loot_cards.reset()
        self.scans, self.clock, self.reports, self.shown = [], [T0], [], []
        self.scan_result, self.queued = {"executed": True, "returned": True}, []
        self.currency_calls, self.currency_queue, self.sleeps = [], [], []
        ledger_config.remember_injected({})
        for target, attribute, value in (
                (store, "PersistentStore", MemoryAccountStore),
                (point, "PersistentStore", MemoryAccountStore),
                (point, "current_session", lambda: SimpleNamespace(source="injected")),
                (point, "sync_from_context", lambda context, where="", stop=True: True),
                (point, "run_bag_scan", self.fake_scan),
                (point, "read_currency", self.fake_currency),
                (point, "_report", self.fake_report),
                (point.time, "time", lambda: self.clock[0]),
                (point.time, "sleep", self.sleeps.append)):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_scan(self, context, *, label, scope, origin, targets="all"):
        self.scans.append({"label": label, "scope": list(scope), "origin": origin, "targets": targets})
        self.clock[0] += 100
        result = self.queued.pop(0) if self.queued else self.scan_result
        if not result["executed"]:
            return {"executed": False, "returned": False, "error": "scan_not_reached"}
        return {"termination": "list_bottom", **result, "scan_id": f"scan{len(self.scans)}", "scope": list(scope)}

    def fake_report(self, label, show=False):
        self.reports.append(label)
        self.shown.append(show)

    def fake_currency(self, context, diamond_detail=None):
        self.currency_calls.append(diamond_detail)
        if self.currency_queue:
            return {**self.currency_full(), **self.currency_queue.pop(0)}
        return self.currency_full()

    @staticmethod
    def currency_full():
        return {"observed_at": "2025-11-15T12:02:00+00:00", "gold": 12345, "diamond": 678, "tonic": 9,
                "diamond_free": None, "diamond_paid": None, "diamond_paid_source": None,
                "icons_found": ["diamond", "gold", "tonic"], "detail_error": None, "detail_closed": None}

    def task(self, entry, task_id, gap=3.0):
        rows = task_journal.rows()
        if rows and rows[-1]["end"] is None:
            task_journal.record(rows[-1]["entry"], rows[-1]["task_id"], "succeeded", now=self.clock[0])
        self.clock[0] += gap
        task_journal.record(entry, task_id, "start", now=self.clock[0])
        self.clock[0] += 20

    def run_point(self, mark, kind, task_id, entry, context=None):
        ok = point.LedgerPoint().run(context or NoConfig(), argv(mark, kind, task_id, entry))
        self.clock[0] += 30
        return ok

    def events(self, kind):
        events = MemoryAccountStore.data.get("arbitrage", {}).get("inventory", {}).get("events", [])
        return [event for event in events if event.get("kind") == kind]

    def test_first_daily_leaves_mark_scans_everything_and_reads_currency(self):
        self.task("Mail_HomePage", 101)
        self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        mark, = self.events("ledger_mark")
        self.assertEqual((mark["mark"], mark["type"], mark["node"]), ("begin", "daily", "Ledger_Node"))
        self.assertEqual(mark["task"], {"entry": "Mail_HomePage", "task_id": 101})
        self.assertEqual(mark["session"], task_journal.SESSION)
        self.assertEqual([row["task_id"] for row in mark["tasks"]], [101])
        scan, = self.scans
        self.assertEqual(scan["scope"], list(ALL_SCOPES))
        self.assertEqual(scan["label"], "记账·日常开头")
        self.assertEqual(scan["origin"]["mark"], "begin")
        self.assertEqual(scan["origin"]["task"]["task_id"], 101)
        currency, = self.events("currency_reading")
        self.assertEqual((currency["scan_id"], currency["label"], currency["gold"]), ("scan1", "记账·日常开头", 12345))

    def test_second_daily_leaves_mark_without_scanning(self):
        self.task("Mail_HomePage", 101)
        self.run_point("begin", "daily", 101, "Mail_HomePage")
        self.task("Pass_HomePage", 102)
        self.run_point("begin", "daily", 102, "Pass_HomePage")
        self.assertEqual(len(self.scans), 1)
        marks = self.events("ledger_mark")
        self.assertEqual([mark["task"]["task_id"] for mark in marks], [101, 102])
        self.assertEqual([row["task_id"] for row in marks[1]["tasks"]], [101, 102])   # 上一条标记以来的任务起止
        self.assertIsNotNone(marks[1]["tasks"][0]["end"])

    def test_k21_collect_marks_carry_the_cycle_records(self):
        MemoryAccountStore.data = {"cycles": {"Story_01_血骑士@g_weekly": "2025-11-15 20:00:00",
                                              "Daily_Mail@daily": "2025-11-15 20:01:00"}}   # 日常的完成记录不带
        self.task("Collect_StartGame_HomePage", 101)
        self.run_point("begin", "collect", 101, "Collect_StartGame_HomePage")
        self.task("Mail_HomePage", 102)
        self.run_point("begin", "daily", 102, "Mail_HomePage")
        collect, daily = self.events("ledger_mark")
        self.assertEqual(collect["cards"], {"Story_01_血骑士@g_weekly": "2025-11-15 20:00:00"})
        self.assertNotIn("cards", daily)

    def test_collect_end_hands_over_the_last_card_before_marking(self):
        self.task("Collect_StartGame_HomePage", 101)
        self.run_point("begin", "collect", 101, "Collect_StartGame_HomePage")
        task = {"entry": "Collect_StartGame_HomePage", "task_id": 101}
        point.loot_cards.enter("Story_01_血骑士", task=task, account="7", now=self.clock[0])
        point.loot_cards.add_point({"time": self.clock[0] + 5, "items": [{"name": "兽肉", "count": 12, "known": True}]},
                                   card="Story_01_血骑士", task=task)
        self.run_point("end", "collect", 101, "Collect_StartGame_HomePage")   # 次数用完，卡没吸完
        events = MemoryAccountStore.data["arbitrage"]["inventory"]["events"]
        kinds = [event["kind"] for event in events]
        self.assertLess(kinds.index("collect_loot"), len(kinds) - 1 - kinds[::-1].index("ledger_mark"))
        loot, = self.events("collect_loot")
        self.assertEqual((loot["card"], loot["end"], loot["items"], loot["task"]),
                         ("Story_01_血骑士", "task_end", {"兽肉": 12}, task))
        self.assertNotIn("account", loot)

    def test_later_position_hands_over_a_stopped_collect(self):
        task = {"entry": "Collect_StartGame_HomePage", "task_id": 101}
        point.loot_cards.enter("Story_01_血骑士", task=task, account="7", now=self.clock[0])
        point.loot_cards.add_point({"time": self.clock[0] + 5, "items": [{"name": "兽肉", "count": 3, "known": True}]},
                                   card="Story_01_血骑士", task=task)
        self.task("Mail_HomePage", 102)
        self.run_point("begin", "daily", 102, "Mail_HomePage")
        loot, = self.events("collect_loot")
        self.assertEqual(loot["end"], "later")

    def test_normal_end_mark_needs_a_begin_in_the_same_task(self):
        self.task("Mail_HomePage", 101)
        self.run_point("begin", "daily", 101, "Mail_HomePage")
        self.task("Collect_StartGame_HomePage", 102)
        self.run_point("end", "collect", 102, "Collect_StartGame_HomePage")       # 任务开头就次数已满
        self.assertEqual([mark["mark"] for mark in self.events("ledger_mark")], ["begin"])
        self.assertEqual(self.scans[-1]["scope"], ["B", "C"])                      # 仍替日常收尾
        self.task("Collect_StartGame_HomePage", 103)
        self.run_point("begin", "collect", 103, "Collect_StartGame_HomePage")
        self.run_point("end", "collect", 103, "Collect_StartGame_HomePage")
        self.assertEqual([mark["mark"] for mark in self.events("ledger_mark")], ["begin", "begin", "end"])
        self.assertEqual(self.scans[-1]["label"], "记账·吸取结束")

    def test_unconfirmed_account_writes_nothing(self):
        self.task("Mail_HomePage", 101)
        asked = []

        def unconfirmed(context, where="", stop=True):
            asked.append(stop)
            return False

        with patch.object(point, "sync_from_context", unconfirmed):
            self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        self.assertEqual((self.events("ledger_mark"), self.scans), ([], []))
        self.assertEqual(asked, [False])                   # 只问一声，不替所在流程停任务

    def test_k16_switch_off_does_nothing_at_all(self):
        self.task("Mail_HomePage", 101)
        ledger_config.remember_injected({"scan": "No"})
        with patch.object(point, "sync_from_context", side_effect=AssertionError("关着时不该碰账号")):
            self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        self.assertEqual((self.events("ledger_mark"), self.scans), ([], []))
        self.assertEqual(point._STATES, {})

    def test_full_range_option_scans_everything_but_only_when_the_rule_says_scan(self):
        ledger_config.remember_injected({"full": "每次全盘"})
        self.task("Arbitrage_Start", 101)
        self.run_point("begin", "shop", 101, "Arbitrage_Start")
        self.run_point("end", "shop", 101, "Arbitrage_Start")                     # 规则只要 A，选项改成全盘
        self.task("Close_HomePage", 102)
        self.run_point("queue_end", None, 102, "Close_HomePage")                  # 规则说不盘：仍不盘
        self.assertEqual([scan["scope"] for scan in self.scans], [list(ALL_SCOPES), list(ALL_SCOPES)])


    def test_report_follows_every_scan_that_ran_unless_switched_off(self):
        self.task("Mail_HomePage", 101)
        self.run_point("begin", "daily", 101, "Mail_HomePage")                    # 盘了 → 出报表
        self.task("Pass_HomePage", 102)
        self.run_point("begin", "daily", 102, "Pass_HomePage")                    # 同类型接着做，不盘 → 不出
        self.assertEqual(self.reports, ["记账·日常开头"])
        self.scan_result = {"executed": False, "returned": False}
        self.task("Arbitrage_Start", 103)
        self.run_point("begin", "shop", 103, "Arbitrage_Start")                   # 该盘但没执行 → 不出
        self.assertEqual(len(self.reports), 1)
        ledger_config.remember_injected({"report": "No"})
        self.scan_result = {"executed": True, "returned": True}
        self.run_point("end", "shop", 103, "Arbitrage_Start")                     # 报表开关关着 → 照盘、不出
        self.assertEqual((len(self.scans), len(self.reports)), (3, 1))

    def test_closing_positions_refresh_the_report_even_without_a_scan(self):
        self.task("Arbitrage_Start", 101)
        self.run_point("begin", "shop", 101, "Arbitrage_Start")
        self.run_point("end", "shop", 101, "Arbitrage_Start")
        self.task("Ledger_Settle_Manual", 102)
        self.run_point("manual", None, 102, "Ledger_Settle_Manual")               # 没有没盘掉的活动：不盘，但出一次报表
        self.assertEqual((len(self.scans), self.reports[-1], len(self.reports)), (2, "记账·手动结账", 3))
        self.task("Close_HomePage", 103)
        self.run_point("queue_end", None, 103, "Close_HomePage")
        self.assertEqual(self.shown, [False, False, True, False])               # 只有手动结账出完自己打开（U61）

    def test_manual_settle_opens_the_report_on_desktop_only(self):
        opened = []
        result = {"days": ["2026-11-15"], "latest": Path("Z:/报表/latest.html"), "summary": "摘要", "entry": None}
        with patch.object(point, "_report", REAL_REPORT), patch.object(point, "_open", opened.append), \
                patch.object(MemoryAccountStore, "CONFIG_DIR", "Z:/存档", create=True), \
                patch.object(point.inventory_report_output, "generate_reports", return_value=result) as generate:
            with patch.object(MemoryAccountStore, "_storage_policy", SimpleNamespace(portable_root=Path("Z:/装")),
                              create=True):
                point._report("记账·商店结束")
                point._report("记账·手动结账", show=True)
                self.assertEqual(generate.call_args.kwargs["entry_dir"], Path("Z:/装"))
            with patch.object(MemoryAccountStore, "_storage_policy", SimpleNamespace(portable_root=None), create=True):
                point._report("记账·手动结账", show=True)                          # 安卓、指定存档目录：不开
        self.assertEqual(opened, [Path("Z:/报表/latest.html")])

    def test_stock_not_fully_read_is_rescanned_once(self):
        self.queued = [{"executed": True, "returned": True,
                        "unknown": {"兽肉": "state_lost", "精炼石": "state_lost", "鸡蛋": "template_missing"}},
                       {"executed": True, "returned": True, "unknown": {}}]
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米", "鸡蛋"}):
            self.task("Mail_HomePage", 101)
            self.run_point("begin", "daily", 101, "Mail_HomePage")
        first, again = self.scans
        self.assertEqual((again["label"], again["scope"], again["targets"]), ("记账·日常开头·重扫", ["A"], ["兽肉"]))
        self.assertEqual(again["origin"], {**first["origin"], "retry_of": "scan1"})
        currency, = self.events("currency_reading")                              # 货币只读一次，记在原来那次名下
        self.assertEqual(currency["scan_id"], "scan1")
        self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan1")

    def test_rescan_only_for_stock_and_only_once(self):
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米"}):
            self.queued = [{"executed": True, "returned": True, "unknown": {"精炼石": "state_lost"}}]   # 只有养成材料没读准
            self.task("Mail_HomePage", 101)
            self.run_point("begin", "daily", 101, "Mail_HomePage")
            self.assertEqual(len(self.scans), 1)
            self.queued = [{"executed": True, "returned": True, "unknown": {"兽肉": "stopped"}}]       # 任务被停：不重扫
            self.task("Arbitrage_Start", 102)
            self.run_point("begin", "shop", 102, "Arbitrage_Start")
            self.assertEqual(len(self.scans), 2)
            self.queued = [{"executed": True, "returned": True, "unknown": {"兽肉": "state_lost"}},
                           {"executed": True, "returned": False, "unknown": {"兽肉": "state_lost"}}]
            self.run_point("end", "shop", 102, "Arbitrage_Start")
        self.assertEqual(len(self.scans), 4)                                       # 重扫还不全也只扫一次
        self.assertEqual(len(self.events("currency_reading")), 3)                  # 重扫没回到原位：收尾位置换到箱庭照样读

    def test_report_failure_is_only_logged(self):
        with patch.object(point, "_report", REAL_REPORT), \
                patch.object(MemoryAccountStore, "CONFIG_DIR", "Z:/不存在", create=True), \
                patch.object(point.inventory_report_output, "generate_reports", side_effect=OSError("磁盘满")):
            self.task("Mail_HomePage", 101)
            self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        self.assertEqual(len(self.scans), 1)

    def test_scan_that_did_not_run_or_return(self):
        self.task("Mail_HomePage", 101)
        self.scan_result = {"executed": False, "returned": False}
        self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        self.assertEqual(self.events("currency_reading"), [])
        self.assertIsNone(point._STATES["7"].last_scan)
        self.scan_result = {"executed": True, "returned": False}                  # 盘了但没回到原位：不读货币
        self.task("Pass_HomePage", 102)
        self.run_point("begin", "daily", 102, "Pass_HomePage")
        self.assertEqual(self.events("currency_reading"), [])
        self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan2")

    def test_scan_cut_off_midway_is_not_counted(self):
        self.queued = [{"executed": True, "returned": True, "termination": "state_lost",
                        "unknown": {"精炼石": "state_lost", "金矿石": "corner_unconfirmed"}}]
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米"}):
            self.task("Mail_HomePage", 101)
            self.run_point("begin", "daily", 101, "Mail_HomePage")
            state = point._STATES["7"]
            self.assertIsNone(state.last_scan)                                     # 不算盘过
            self.assertEqual([item["kind"] for item in state.pending], ["daily"])  # 日常的活动还挂着
            self.assertEqual(len(self.events("currency_reading")), 1)              # 回到了原位：货币照读
            self.assertEqual(self.reports, ["记账·日常开头"])                        # 读到的那部分进了凭证：报表照出
            self.task("Collect_StartGame_HomePage", 102)
            self.run_point("begin", "collect", 102, "Collect_StartGame_HomePage")
        self.assertEqual(self.scans[-1]["scope"], list(ALL_SCOPES))                # 下一个位置接着补：全盘
        self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan2")

    def test_every_cut_off_reason_counts_as_not_scanned(self):
        for termination in ("stopped", "timeout", "state_lost", "error", "page_limit", None):
            with self.subTest(termination=termination):
                point._STATES.clear()
                self.queued = [{"executed": True, "returned": True, "termination": termination, "unknown": {}}]
                self.task("Mail_HomePage", 101)
                self.run_point("begin", "daily", 101, "Mail_HomePage")
                self.assertIsNone(point._STATES["7"].last_scan)
        for termination in ("all_found", "list_bottom", "section_end"):
            with self.subTest(termination=termination):
                point._STATES.clear()
                self.queued = [{"executed": True, "returned": True, "termination": termination,
                                "unknown": {"兽肉": "detail_unreadable"}}]               # 有没读准的照样算盘过
                self.task("Mail_HomePage", 101)
                self.run_point("begin", "daily", 101, "Mail_HomePage")
                self.assertIsNotNone(point._STATES["7"].last_scan)

    def test_stock_only_scan_cut_off_but_refilled_by_rescan_counts(self):
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米", "鸡蛋"}):
            self.task("Collect_StartGame_HomePage", 101)
            self.run_point("begin", "collect", 101, "Collect_StartGame_HomePage")
            self.queued = [{"executed": True, "returned": True, "termination": "state_lost",
                            "unknown": {"兽肉": "state_lost", "米": "corner_unconfirmed", "鸡蛋": "template_missing"}},
                           {"executed": True, "returned": True, "unknown": {}}]
            self.run_point("end", "collect", 101, "Collect_StartGame_HomePage")
            self.assertEqual(self.scans[-1]["targets"], ["兽肉", "米"])
            self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan2")    # 断掉的都补上了：算盘过
            self.task("Arbitrage_Start", 102)
            self.run_point("begin", "shop", 102, "Arbitrage_Start")
            self.assertEqual(len(self.scans), 3)                                   # 商店开头一盘两用
            self.queued = [{"executed": True, "returned": True, "termination": "state_lost",
                            "unknown": {"兽肉": "state_lost"}},
                           {"executed": True, "returned": True, "unknown": {"兽肉": "detail_unreadable"}}]
            self.run_point("end", "shop", 102, "Arbitrage_Start")
        self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan2")        # 重扫也没补上：不算
        self.assertEqual([item["kind"] for item in point._STATES["7"].pending], ["shop"])

    def test_closing_scan_cut_off_is_rescanned_across_all_scopes(self):
        """U69：收尾那一盘半路断了，后面没有下一盘——没走到的各类当场一起重扫一次；补齐了就算盘过。"""
        self.clock[0] = T0 + 12 * 3600 - 400                                       # 08:53 KST 进店
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米"}):
            self.task("Arbitrage_Start", 101)
            self.run_point("begin", "shop", 101, "Arbitrage_Start")
            self.clock[0] += 900                                                   # 收尾跨过 09:00 KST：全盘
            self.queued = [{"executed": True, "returned": True, "termination": "state_lost",
                            "unknown": {"精炼石": "state_lost", "兽肉": "state_lost", "鸡蛋": "template_missing"}},
                           {"executed": True, "returned": True, "unknown": {}}]
            self.run_point("end", "shop", 101, "Arbitrage_Start")
        whole, again = self.scans[-2:]
        self.assertEqual(whole["scope"], list(ALL_SCOPES))
        self.assertEqual((again["scope"], again["targets"]), (list(ALL_SCOPES), ["兽肉", "精炼石"]))
        self.assertEqual(again["origin"]["retry_of"], "scan2")
        self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan2")        # 补齐了：算盘过
        self.assertEqual(point._STATES["7"].pending, [])

    def test_begin_scan_cut_off_rescans_only_stock(self):
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米"}):
            self.queued = [{"executed": True, "returned": True, "termination": "state_lost",
                            "unknown": {"精炼石": "state_lost", "兽肉": "state_lost"}},
                           {"executed": True, "returned": True, "unknown": {}}]
            self.task("Mail_HomePage", 101)
            self.run_point("begin", "daily", 101, "Mail_HomePage")
        self.assertEqual((self.scans[-1]["scope"], self.scans[-1]["targets"]), (["A"], ["兽肉"]))   # 开头位置后面还有盘点
        self.assertIsNone(point._STATES["7"].last_scan)                             # B 没补上：不算

    def test_stopped_stock_scan_is_not_rescued_by_other_rescans(self):
        with patch.object(point, "scope_names", lambda context, scope: {"兽肉", "米"}):
            self.task("Collect_StartGame_HomePage", 101)
            self.run_point("begin", "collect", 101, "Collect_StartGame_HomePage")
            self.queued = [{"executed": True, "returned": True, "termination": "stopped",
                            "unknown": {"兽肉": "stopped", "米": "detail_unreadable"}},
                           {"executed": True, "returned": True, "unknown": {}}]
            self.run_point("end", "collect", 101, "Collect_StartGame_HomePage")
        self.assertEqual(self.scans[-1]["targets"], ["米"])                         # 被停的不重扫
        self.assertEqual(point._STATES["7"].last_scan["scan_id"], "scan1")        # 补上的不是断掉的那几样：不算

    def test_currency_missing_is_read_again_in_place(self):
        self.currency_queue = [{"gold": None, "tonic": None}, {"tonic": None, "diamond": None}]
        self.task("Mail_HomePage", 101)
        self.run_point("begin", "daily", 101, "Mail_HomePage")
        currency, = self.events("currency_reading")                               # 拼成一条，挂在这次盘点名下
        self.assertEqual((currency["gold"], currency["diamond"], currency["tonic"]), (12345, 678, 9))
        self.assertEqual((self.currency_calls, self.sleeps), ([None, False, False], [1.0, 1.0]))

    def test_begin_position_never_leaves_the_page_for_currency(self):
        self.currency_queue = [{"gold": None}] * 5
        context = Roaming()
        self.task("Mail_HomePage", 101)
        self.run_point("begin", "daily", 101, "Mail_HomePage", context)
        self.assertEqual(context.ran, [])                                          # 后面还有流程：不换页面
        currency, = self.events("currency_reading")
        self.assertIsNone(currency["gold"])
        self.assertEqual(len(self.currency_calls), point.CURRENCY_TRIES)

    def test_closing_position_goes_to_sandbox_then_home_until_currency_is_read(self):
        context = Roaming()
        self.task("Arbitrage_Start", 101)
        self.run_point("begin", "shop", 101, "Arbitrage_Start", context)
        self.currency_queue = [{"gold": None}] * (point.CURRENCY_TRIES * 2) + [{"tonic": None}]
        self.run_point("end", "shop", 101, "Arbitrage_Start", context)
        self.assertEqual(context.ran, ["Global_ToSandBox", "Global_ToHomePage"])  # 原地、箱庭都没读到金币，再回主界面
        currency = self.events("currency_reading")[-1]
        self.assertEqual((currency["scan_id"], currency["gold"], currency["tonic"]), ("scan2", 12345, 9))
        self.assertEqual(len(self.currency_calls), 1 + point.CURRENCY_TRIES * 2 + 1)

    def test_closing_scan_that_did_not_return_reads_currency_in_the_sandbox(self):
        context = Roaming()
        self.task("Arbitrage_Start", 101)
        self.run_point("begin", "shop", 101, "Arbitrage_Start", context)
        self.scan_result = {"executed": True, "returned": False}
        self.run_point("end", "shop", 101, "Arbitrage_Start", context)
        self.assertEqual(context.ran, ["Global_ToSandBox"])                       # 页面不明：不在原地读
        self.assertEqual(self.events("currency_reading")[-1]["scan_id"], "scan2")
        self.assertEqual(len(self.currency_calls), 2)

    def test_stopping_task_does_not_roam_for_currency(self):
        context = Roaming(stopping=True)
        self.task("Arbitrage_Start", 101)
        self.currency_queue = [{"gold": None}] * 10
        self.run_point("begin", "shop", 101, "Arbitrage_Start", context)
        self.run_point("end", "shop", 101, "Arbitrage_Start", context)
        self.assertEqual(context.ran, [])
        self.assertIsNone(self.events("currency_reading")[-1]["gold"])

    def test_errors_never_break_the_host_flow(self):
        self.task("Mail_HomePage", 101)
        self.assertTrue(point.LedgerPoint().run(NoConfig(), argv("pause", None, 101, "Mail_HomePage")))
        self.assertTrue(point.LedgerPoint().run(NoConfig(), argv("begin", None, 101, "Mail_HomePage")))
        with patch.object(point, "run_bag_scan", side_effect=RuntimeError("进包失败")):
            self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        self.assertEqual(self.scans, [])

    def test_accounts_do_not_share_state(self):
        self.task("Mail_HomePage", 101)
        self.run_point("begin", "daily", 101, "Mail_HomePage")
        self.task("Pass_HomePage", 102)
        with patch.object(MemoryAccountStore, "_current_account_id", "8"):
            self.run_point("begin", "daily", 102, "Pass_HomePage")
        self.assertEqual([scan["scope"] for scan in self.scans], [list(ALL_SCOPES), list(ALL_SCOPES)])


class LedgerConfigTests(unittest.TestCase):
    INTERFACE = {"option": {
        "记账盘点": {"type": "switch", "default_case": "Yes", "cases": [{"name": "Yes"}, {"name": "No"}]},
        "记账盘点范围": {"type": "select", "cases": [{"name": "按规则"}, {"name": "每次全盘"}]},
        "记账报表": {"type": "switch", "default_case": "Yes", "cases": [{"name": "Yes"}, {"name": "No"}]}}}

    def task(self, *options):
        return {"TaskItems": [{"entry": "Daily_HomePage", "option": [{"name": "记账盘点", "selected_case": "No"}]},
                              {"entry": ledger_config.TASK_ENTRY, "option": list(options)}]}

    def test_k20_instance_file_choices_and_defaults(self):
        parse = ledger_config.parse_instance
        self.assertEqual(parse(self.task(), self.INTERFACE), {"scan": True, "full": False, "report": True})
        self.assertEqual(parse(self.task({"name": "记账盘点", "selected_case": "No"}), self.INTERFACE)["scan"], False)
        self.assertEqual(parse(self.task({"name": "记账盘点", "index": 1}), self.INTERFACE)["scan"], False)   # 旧客户端存序号
        self.assertEqual(parse(self.task({"name": "记账盘点范围", "selected_case": "每次全盘"},
                                         {"name": "记账报表", "selected_case": "No"}), self.INTERFACE),
                         {"scan": True, "full": True, "report": False})
        # 没有这个任务、读不到 interface、选项里是已经不存在的 case：都跟默认
        self.assertEqual(parse({"TaskItems": []}, self.INTERFACE), ledger_config.DEFAULTS)
        self.assertEqual(parse(self.task({"name": "记账盘点", "selected_case": "No"}), None)["scan"], True)
        self.assertEqual(parse(self.task({"name": "记账盘点", "selected_case": "也许"}), self.INTERFACE)["scan"], True)
        self.assertEqual(parse(None, None), ledger_config.DEFAULTS)

    def test_k20_other_clients_follow_the_config_task_or_defaults(self):
        ledger_config.remember_injected({})
        session = SimpleNamespace(source="injected")
        self.assertEqual(ledger_config.ledger_options(session), ledger_config.DEFAULTS)     # 没跑过配置任务
        ledger_config.remember_injected({"scan": "No", "full": "", "report": "Yes"})
        self.assertEqual(ledger_config.ledger_options(session), {"scan": False, "full": False, "report": True})
        ledger_config.remember_injected({})

    def test_unreadable_instance_file_keeps_last_good_then_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config" / "instances").mkdir(parents=True)
            (root / "interface.json").write_text(json.dumps(self.INTERFACE, ensure_ascii=False), encoding="utf-8")
            path = root / "config" / "instances" / "one.json"
            session = SimpleNamespace(source="mfaa", project_root=root, instance_id="one")
            with patch.object(ledger_config.time, "sleep", lambda seconds: None):
                self.assertEqual(ledger_config.ledger_options(session), ledger_config.DEFAULTS)   # 文件还不存在
                path.write_text(json.dumps(self.task({"name": "记账盘点", "selected_case": "No"}), ensure_ascii=False),
                                encoding="utf-8")
                self.assertEqual(ledger_config.ledger_options(session)["scan"], False)
                path.write_text("{ 半截", encoding="utf-8")
                self.assertEqual(ledger_config.ledger_options(session)["scan"], False)            # 沿用上一次读成的
                bad = SimpleNamespace(source="mfaa", project_root=root, instance_id="../one")
                self.assertEqual(ledger_config.ledger_options(bad), ledger_config.DEFAULTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
