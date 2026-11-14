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
from utils import task_journal  # noqa: E402
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
        for target, attribute, value in (
                (store, "PersistentStore", MemoryAccountStore),
                (point, "PersistentStore", MemoryAccountStore),
                (point, "sync_from_context", lambda context, where="": True),
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
        with patch.object(point, "sync_from_context", lambda context, where="": False):
            self.assertTrue(self.run_point("begin", "daily", 101, "Mail_HomePage"))
        self.assertEqual((self.events("ledger_mark"), self.scans), ([], []))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
