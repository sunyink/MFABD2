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
        self.scans, self.clock = [], [T0]
        self.scan_result = {"executed": True, "returned": True}
        ledger_config.remember_injected({})
        for target, attribute, value in (
                (store, "PersistentStore", MemoryAccountStore),
                (point, "PersistentStore", MemoryAccountStore),
                (point, "current_session", lambda: SimpleNamespace(source="injected")),
                (point, "sync_from_context", lambda context, where="", stop=True: True),
                (point, "run_bag_scan", self.fake_scan),
                (point, "read_currency", self.fake_currency),
                (point.time, "time", lambda: self.clock[0])):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_scan(self, context, *, label, scope, origin):
        self.scans.append({"label": label, "scope": list(scope), "origin": origin})
        self.clock[0] += 100
        if not self.scan_result["executed"]:
            return {"executed": False, "returned": False, "error": "scan_not_reached"}
        return {**self.scan_result, "scan_id": f"scan{len(self.scans)}", "scope": list(scope)}

    def fake_currency(self, context):
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

    def run_point(self, mark, kind, task_id, entry):
        ok = point.LedgerPoint().run(NoConfig(), argv(mark, kind, task_id, entry))
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
