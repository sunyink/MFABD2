"""Real Maa node dispatch with temporary archives and a controller that cannot click."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit
from maa.custom_action import CustomAction

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer
with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls):
    from action import inventory_archive as action, arbitrage_flow as flow
from utils import arbitrage_store as store, inventory_archive as history
from verify_inventory_archive import ArchiveFixture, MemoryStore
from verify_arbitrage_purchase_cycle import OfflineController


PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))


class ActionFixture(ArchiveFixture):
    def setUp(self):
        super().setUp()
        for mock in (patch.object(action, "PersistentStore", MemoryStore),
                     patch.object(action, "sync_from_context", return_value=True),
                     patch.dict(action._RUNS, clear=True)):
            mock.start()
            self.addCleanup(mock.stop)

    def call(self, phase="begin", task_id=1, **params):
        args = {"series": "arbitrage", "position": "A" if phase == "begin" else "B",
                "keep": "first" if phase == "begin" else "last", "phase": phase, **params}
        argv = NS(task_detail=NS(task_id=task_id), custom_action_param=json.dumps(args))
        self.assertTrue(action.InventoryArchive().run(None, argv))


class ActionTests(ActionFixture):
    def test_reentry_and_duplicate_end_do_not_create_another_run(self):
        self.call()
        self.call()
        self.call("end")
        self.call("end")
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.state()["runs"]), 1)

    def test_account_switch_cannot_write_b_into_new_account(self):
        self.call()
        MemoryStore._current_account_id = "other"
        MemoryStore.data = {}
        self.call("end")
        self.assertEqual(self.rows(), [])
        self.assertEqual(MemoryStore.data, {})

    def test_missing_begin_and_primary_write_failure_cannot_produce_b(self):
        self.call("end")
        MemoryStore.fail_save = True
        self.call(task_id=2)
        MemoryStore.fail_save = False
        self.call("end", task_id=2)
        self.assertEqual(self.rows(), [])

    def test_unreadable_save_does_not_archive_empty_inventory(self):
        MemoryStore._degraded_readonly = True
        self.call()
        self.assertEqual(self.rows(), [])

    def test_invalid_params_do_not_block_business(self):
        self.call(position="", keep="wrong")
        self.assertEqual(self.rows(), [])

    def test_restart_keeps_prior_open_run_and_first_a(self):
        store.set_inventory_quantities({"蜂蜜": 5}, "test")
        self.call()
        first = self.state()["positions"]["arbitrage"]["A"]["run_id"]
        action._RUNS.clear()
        history._WRITERS.clear()
        store.set_inventory_quantities({"蜂蜜": 8}, "test")
        self.call(task_id=2)
        self.call("end", task_id=2)
        data = self.state()
        self.assertEqual(data["positions"]["arbitrage"]["A"]["items"]["蜂蜜"]["quantity"], 5)
        self.assertEqual(data["runs"][first]["status"], "no_end_evidence")


class NativeTests(ActionFixture):
    def exercise(self, *, active=True, stop=False, sandbox=True, double_entry=False):
        self.assertTrue(Toolkit.init_option(self.temp.name, {"logging": False}))
        self.assertTrue(Tasker.set_save_on_error(False))
        resource = Resource()
        self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
        self.assertTrue(resource.register_custom_action("ArbitrageStagePrepare", flow.ArbitrageStagePrepare()))
        self.assertTrue(resource.register_custom_action("InventoryArchive", action.InventoryArchive()))
        observed = []

        class Work(CustomAction):
            def run(self, context, argv):
                observed.append(argv.node_name)
                # A must already exist before the first business action.
                data = history._writer(MemoryStore)._load_day(history.day_of(history.now()))
                if "A" not in data["positions"].get("arbitrage", {}):
                    raise AssertionError("business entered before A")
                store.set_inventory_quantities({"蜂蜜": 7}, "native_test")
                return True

        resource.register_custom_action("ArchiveTestWork", Work())
        overrides = {name: {"enabled": active, "recognition": "DirectHit", "action": "Custom",
                             "custom_action": "ArchiveTestWork", "custom_action_param": {},
                             "max_hit": 1, "next": [], "on_error": ["Global_Null"],
                             "pre_delay": 0, "post_delay": 0, "rate_limit": 0}
                     for name in flow.STAGES}
        if stop:
            overrides[flow.STAGES[1]]["next"] = ["Arbitrage_Replenish_Stop"]
        overrides.update({
            "Arbitrage_Merchant_Ico": {"recognition": "DirectHit", "action": "DoNothing",
                                        "next": ["Arbitrage_Action_Hub"], "pre_delay": 0, "post_delay": 0},
            "Arbitrage_Action_Hub": {"action": "DoNothing", "pre_delay": 0, "post_delay": 0,
                                      "timeout": 1, "rate_limit": 0},
            "Arbitrage_Merchant_Close_Ck": {"enabled": False},
            "Global_BackPageHub_Once": {"enabled": False},
            "Global_ToSandBox": {"recognition": "DirectHit", "inverse": False, "action": "DoNothing",
                                  "next": [], "max_hit": 1, "pre_delay": 0, "post_delay": 0},
            "Arbitrage_Archive_End": {"timeout": 1, "rate_limit": 0},
        })
        if sandbox:
            overrides["Global_ToSandBox_Enter"] = {"recognition": "DirectHit", "inverse": False}
        if double_entry:
            # Re-enter the same begin node through a different branch in this task.
            overrides[flow.STAGES[0]]["next"] = ["[JumpBack]Arbitrage_Archive_Begin"]
        self.assertTrue(resource.override_pipeline(overrides))
        controller = OfflineController()
        self.assertTrue(controller.post_connection().wait().succeeded)
        tasker = Tasker()
        tasker.bind(resource, controller)
        try:
            detail = tasker.post_task("Arbitrage_Start").wait().get()
            return detail, observed
        finally:
            tasker.post_stop().wait()
            del tasker
            del controller
            resource.clear()

    def test_native_all_stages_then_end_and_reentry(self):
        detail, observed = self.exercise(double_entry=True)
        self.assertTrue(detail.status.succeeded)
        self.assertEqual(observed, list(flow.STAGES))
        data = self.state()
        self.assertEqual(set(data["positions"]["arbitrage"]), {"A", "B"})
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["positions"]["arbitrage"]["B"]["items"]["蜂蜜"]["quantity"], 7)

    def test_native_all_disabled_writes_nothing(self):
        detail, observed = self.exercise(active=False)
        self.assertTrue(detail.status.succeeded)
        self.assertEqual(observed, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(MemoryStore.data, {})

    def test_native_stop_keeps_a_without_b(self):
        _, observed = self.exercise(stop=True)
        self.assertEqual(observed, list(flow.STAGES[:2]))
        self.assertEqual(set(self.state()["positions"]["arbitrage"]), {"A"})

    def test_native_unconfirmed_cleanup_does_not_write_b(self):
        self.exercise(sandbox=False)
        self.assertEqual(set(self.state()["positions"]["arbitrage"]), {"A"})


if __name__ == "__main__":
    unittest.main()
