"""采购扫描周标记回归：实际管路与内核、内存存档、空白控制器，不连接游戏。

运行：python -B tools/verify_arbitrage_purchase_cycle.py -v
"""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

# 在进程内直接调用真实内核；业务类稍后向 Resource 注册，不启动 AgentServer。
Library.version()
from maa.agent.agent_server import AgentServer

with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "_set_api_properties"), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls):
    from action import arbitrage_buy_list as buy
    from action import cartridge_lib as cycles
    from action.pipeline_manager import PatchPipeline
    from action.smart_action import SmartAction
from utils import arbitrage_purchase_lists as lists, arbitrage_store as store
from utils.persistent_store import PersistentStore


PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
INTERFACE = json.loads((ROOT / "assets/interface.json").read_text(encoding="utf-8"), strict=False)
CATALOG = lists.load_purchase_catalog()
TABLE, TABLE_ERRORS = lists.resolve_purchase_table(PIPE[lists.DATA_NODE]["attach"], CATALOG, {})
assert not TABLE_ERRORS, TABLE_ERRORS
CHECK = "Arbitrage_Buy_CheckCycle"
MARK = "Arbitrage_Buy_ScanCycleMark"
CYCLE = PIPE[CHECK]["custom_recognition_param"]
KEY = f"{CYCLE['card_name']}@{CYCLE['cycle_type']}"
FIRST = next(iter(TABLE))


class OfflineController(CustomController):
    swipes = 0

    def connect(self): return True
    def request_uuid(self): return "arbitrage-purchase-cycle-offline"
    def get_features(self): return 0
    def screencap(self): return np.zeros((720, 1280, 3), dtype=np.uint8)
    def start_app(self, *args): return False
    def stop_app(self, *args): return False
    def click(self, *args): return False
    def touch_down(self, *args): return False
    def touch_move(self, *args): return False
    def touch_up(self, *args): return False
    def click_key(self, *args): return False
    def input_text(self, *args): return False
    def key_down(self, *args): return False
    def key_up(self, *args): return False

    def swipe(self, *args):
        self.swipes += 1
        return True


class MemoryContext:
    def __init__(self):
        self.nodes = deepcopy(PIPE)

    def get_node_object(self, name):
        return NS(attach=self.nodes[name].get("attach", {}))

    def get_node_data(self, name):
        return self.nodes[name]

    def override_pipeline(self, values):
        for name, changes in values.items():
            self.nodes[name].update(changes)
        return True


class PurchaseCycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logdir = Path(tempfile.mkdtemp(prefix="mfabd2-purchase-cycle-"))
        Tasker.set_log_dir(cls.logdir)
        cls.resource = Resource()
        assert cls.resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded
        cls.controller = OfflineController()
        assert cls.controller.post_connection().wait().succeeded
        cls.tasker = Tasker()
        assert cls.tasker.bind(cls.resource, cls.controller)

    def setUp(self):
        self.data = {"arbitrage": {"purchase_alignments": {
            card: {"items": sorted(items), "name_version": lists.PURCHASE_NAME_VERSION}
            for card, items in TABLE.items()}}}
        self.writes = []
        self.events = []
        self.scanned = []
        self.exhausted = False
        self.failed_scan = False
        self.mark_failed = False
        self.lose_account = False
        self.lose_run = False
        self.purchase_failed = False
        self.continued = 0
        self.controller.swipes = 0
        self.context = MemoryContext()
        self.argv = NS(task_detail=NS(task_id=99), custom_action_param={})
        for mocked in (
            patch.object(PersistentStore, "load", side_effect=lambda: deepcopy(self.data)),
            patch.object(PersistentStore, "save", side_effect=self.save),
            patch.object(PersistentStore, "_current_account_id", "cycle-test"),
            patch.object(PersistentStore, "_account_ready", True),
            patch.object(buy, "sync_from_context", return_value=True),
            patch.object(cycles, "sync_from_context", return_value=True),
            patch.dict(lists._RUNS, clear=True),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def save(self, data):
        self.data = deepcopy(data)
        self.writes.append(deepcopy(data))
        return True

    def fresh_cycle(self):
        self.data[KEY] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def run_native(self, every_run=False, dirty_marker=False, allow_unverified=True, extra=None):
        owner = self
        traverse = self.exhausted or self.lose_account or self.lose_run

        class Scan(CustomAction):
            def run(self, context, argv):
                owner.events.append("scan")
                run = lists.get_purchase_run(argv.task_detail.task_id)
                owner.scanned.append(set(run["pending"]))
                for card in list(run["pending"]):
                    if owner.exhausted and card == FIRST:
                        continue
                    if owner.failed_scan and card == FIRST:
                        run["pending"].remove(card)
                        run["failed_cards"][card] = "test: cannot verify"
                        continue
                    assert store.save_purchase_alignment(card, run["table"][card])
                    run["pending"].remove(card)
                if traverse:
                    selectors = {row["selector"]: {"enabled": False} for row in CATALOG.values()}
                    first = CATALOG[FIRST]["selector"]
                    if owner.exhausted:
                        selectors[first] = {"enabled": True, "recognition": "DirectHit", "action": "DoNothing",
                                            "pre_delay": 0, "post_delay": 0, "post_wait_freezes": 0,
                                            "timeout": 1, "next": [first]}
                    assert context.override_pipeline(selectors)
                if owner.lose_account:
                    PersistentStore._current_account_id = "other"
                if owner.lose_run:
                    lists.clear_purchase_run(argv.task_detail.task_id)
                return True

        class Mark(cycles.MarkCompleteAction):
            def run(self, context, argv):
                owner.events.append("mark")
                if owner.mark_failed:
                    return False
                return super().run(context, argv)

        class Buy(CustomAction):
            def run(self, context, argv):
                owner.events.append("buy")
                return not owner.purchase_failed

        class Continue(CustomAction):
            def run(self, context, argv):
                owner.continued += 1
                return True

        for name, action in (
            ("PatchPipeline", PatchPipeline()), ("ArbitrageBuyListPrepare", buy.ArbitrageBuyListPrepare()),
            ("ArbitrageBuyScanPrepare", buy.ArbitrageBuyScanPrepare()), ("ArbitrageBuyReady", buy.ArbitrageBuyReady()),
            ("ArbitrageBuyComplete", buy.ArbitrageBuyComplete()),
            ("MarkComplete", Mark()), ("SmartAction", SmartAction()),
            ("test_cycle_scan", Scan()), ("test_cycle_buy", Buy()), ("test_cycle_continue", Continue()),
        ):
            self.assertTrue(self.resource.register_custom_action(name, action))
        for name, recognition in (
            ("CheckCoolDown", cycles.CheckCoolDownRecognition()),
            ("ArbitrageBuyNeedsScan", buy.ArbitrageBuyNeedsScan()),
            ("ArbitrageBuyScanFinished", buy.ArbitrageBuyScanFinished()),
            ("ArbitrageBuyRecordReady", buy.ArbitrageBuyRecordReady()),
        ):
            self.assertTrue(self.resource.register_custom_recognition(name, recognition))
        option = INTERFACE["option"]["购买收藏扫描（每周|每次）"]
        chosen = next(case for case in option["cases"] if case["name"] == ("Yes" if every_run else "No"))
        overrides = deepcopy(chosen["pipeline_override"])
        policy = INTERFACE["option"]["收藏未核实仍继续购买"]
        selected = next(case for case in policy["cases"] if case["name"] == ("Yes" if allow_unverified else "No"))
        overrides.update(deepcopy(selected["pipeline_override"]))
        overrides.update({
            "test_purchase_flow": {"next": ["[JumpBack]Arbitrage_BuyItem", "test_purchase_after"],
                                   "pre_delay": 0, "post_delay": 0},
            "test_purchase_after": {"action": "Custom", "custom_action": "test_cycle_continue",
                                    "pre_delay": 0, "post_delay": 0},
            "Arbitrage_Buy_Open": {"next": ["Arbitrage_Buy_Button"], "pre_delay": 0, "post_delay": 0},
            "Arbitrage_Buy_Button": {"recognition": "DirectHit", "pre_delay": 0, "post_delay": 0},
            # 空白控制器不模拟商店页面；只验证实际收尾分支能够返回外层。
            "Arbitrage_ShopOut": {"recognition": "DirectHit", "next": [], "pre_delay": 0, "post_delay": 0},
            "Arbitrage_PackList_ResetEnter": {
                "recognition": "DirectHit", "action": "Custom", "custom_action": "test_cycle_scan",
                "pre_delay": 0, "post_delay": 0,
                "next": ["Arbitrage_Buy_Select_Str" if traverse else "Arbitrage_Buy_Select_Finished"],
            },
            "Arbitrage_Buy_Select_Str": {"recognition": "DirectHit", "pre_delay": 0, "post_delay": 0},
            "Arbitrage_Favorit_Buy": {
                "recognition": "DirectHit", "action": "Custom", "custom_action": "test_cycle_buy",
                "pre_delay": 0, "post_delay": 0, "post_wait_freezes": 0, "next": [],
            },
        })
        if dirty_marker:
            overrides[MARK] = {"enabled": True}
        if traverse:
            swipe = deepcopy(PIPE["Arbitrage_Select_Swip"]["custom_action_param"])
            swipe["settle_delay"] = 0
            swipe["proxy_override"].update(duration=1, end_hold=0, post_delay=0)
            overrides["Arbitrage_Select_Swip"] = {
                "recognition": "DirectHit", "custom_action_param": swipe,
                "pre_delay": 0, "post_delay": 0, "post_wait_freezes": 0,
            }
        for name, fields in (extra or {}).items():
            overrides.setdefault(name, {}).update(fields)
        timer = threading.Timer(15, self.tasker.post_stop)
        timer.start()
        try:
            result = self.tasker.post_task("test_purchase_flow", overrides).wait().get()
        finally:
            timer.cancel()
        self.assertTrue(result.status.succeeded)
        return result

    def test_legacy_purchase_mark_does_not_skip_first_scan(self):
        self.data["Arbitrage_BuyItems@g_weekly"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.run_native()
        self.assertEqual(self.events, ["scan", "mark", "buy"])
        self.assertEqual(self.scanned, [set(TABLE)])
        self.assertIn(KEY, self.data)

    def test_same_week_unchanged_skips_scan_and_resets_marker(self):
        self.fresh_cycle()
        self.run_native(dirty_marker=True)
        self.assertEqual(self.events, ["buy"])
        self.assertEqual(self.writes, [])

    def test_expired_cycle_rescans_all_cards(self):
        self.data[KEY] = "2000-01-01 00:00:00"
        self.run_native()
        self.assertEqual(self.events, ["scan", "mark", "buy"])
        self.assertEqual(self.scanned, [set(TABLE)])

    def test_every_run_scans_even_when_weekly_marker_is_fresh(self):
        self.fresh_cycle()
        self.run_native(every_run=True)
        self.assertEqual(self.events, ["scan", "mark", "buy"])
        self.assertEqual(self.scanned, [set(TABLE)])

    def test_changed_list_scans_only_that_card_without_weekly_mark(self):
        self.fresh_cycle()
        self.data["arbitrage"]["purchase_alignments"][FIRST] = {"items": ["旧商品"]}
        stamp = self.data[KEY]
        self.run_native()
        self.assertEqual(self.events, ["scan", "buy"])
        self.assertEqual(self.scanned, [{FIRST}])
        self.assertEqual(self.data[KEY], stamp)

    def test_exhausted_card_and_three_unchanged_swipes_still_mark_once(self):
        self.exhausted = True
        result = self.run_native()
        first = CATALOG[FIRST]["selector"]
        self.assertEqual(sum(node.name == first for node in result.nodes), PIPE[first]["max_hit"])
        self.assertEqual(self.controller.swipes, 3)
        self.assertEqual(self.events, ["scan", "mark", "buy"])
        self.assertNotIn(FIRST, self.data["arbitrage"]["purchase_alignments"])
        self.events.clear()
        self.scanned.clear()
        self.exhausted = False
        self.run_native()
        self.assertEqual(self.events, ["scan", "buy"])
        self.assertEqual(self.scanned, [{FIRST}])

    def test_full_scan_preparation_failure_cannot_enable_marker(self):
        with patch.object(store, "invalidate_purchase_alignments", return_value=False):
            self.run_native()
        self.assertEqual(self.events, ["buy"])
        self.assertNotIn(KEY, self.data)
        self.assertEqual(self.continued, 1)
        run = next(iter(lists._RUNS.values()))
        self.assertEqual(run["table"], TABLE)
        self.assertTrue(run["scan_error"])

    def test_full_scan_preparation_failure_strict_skips_purchase(self):
        with patch.object(store, "invalidate_purchase_alignments", return_value=False):
            self.run_native(allow_unverified=False)
        self.assertEqual(self.events, [])
        self.assertNotIn(KEY, self.data)
        self.assertEqual(self.continued, 1)

    def test_exhausted_exit_strict_still_marks_and_continues(self):
        self.exhausted = True
        result = self.run_native(allow_unverified=False)
        self.assertEqual(self.events, ["scan", "mark"])
        self.assertEqual(self.controller.swipes, 3)
        self.assertNotIn("Arbitrage_Buy_Select_Finished", [node.name for node in result.nodes])
        self.assertEqual(self.continued, 1)

    def test_finished_exit_failed_card_uses_selected_policy(self):
        self.failed_scan = True
        result = self.run_native(allow_unverified=False)
        self.assertIn("Arbitrage_Buy_Select_Finished", [node.name for node in result.nodes])
        self.assertEqual(self.events, ["scan", "mark"])
        self.assertEqual(self.continued, 1)
        self.events.clear()
        self.run_native(every_run=True)
        self.assertEqual(self.events, ["scan", "mark", "buy"])

    def test_list_failure_policy_does_not_reenter_preparation(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed), patch.object(buy, "resolve_purchase_table", side_effect=ValueError("bad data")):
                self.events.clear()
                result = self.run_native(allow_unverified=allowed)
                self.assertEqual(self.events, ["buy"] if allowed else [])
                self.assertEqual(sum(node.name == "Arbitrage_Buy_ListPrepare" for node in result.nodes), 1)
                self.assertNotIn("Arbitrage_Buy_CheckCycle", [node.name for node in result.nodes])
        self.assertEqual(self.continued, 2)

    def test_missing_run_after_scan_falls_back_without_mark(self):
        self.lose_run = True
        self.run_native()
        self.assertEqual(self.events, ["scan", "buy"])
        self.assertNotIn(KEY, self.data)
        self.assertEqual(self.continued, 1)

    def test_account_change_after_scan_does_not_mark_other_account(self):
        self.lose_account = True
        self.run_native()
        self.assertEqual(self.events, ["scan", "buy"])
        self.assertNotIn(KEY, self.data)
        self.assertFalse(lists._RUNS)
        self.assertEqual(self.continued, 1)

    def test_sync_failure_bypasses_all_store_access(self):
        with patch.object(buy, "sync_from_context", return_value=False), \
             patch.object(store, "get_purchase_alignments") as read:
            self.run_native()
        read.assert_not_called()
        self.assertEqual(self.events, ["buy"])
        self.assertFalse(self.writes)
        self.assertEqual(self.continued, 1)

    def test_mark_write_failure_does_not_block_purchase(self):
        self.mark_failed = True
        self.run_native(allow_unverified=False)
        self.assertEqual(self.events, ["scan", "mark", "buy"])
        self.assertNotIn(KEY, self.data)
        self.assertEqual(self.continued, 1)

    def test_completion_record_failure_still_cleans_up(self):
        with patch.object(store, "market_day", side_effect=ValueError("record unavailable")) as record:
            result = self.run_native(extra={"Arbitrage_Favorit_Buy": {"next": ["Arbitrage_Buy_Select_End"]}})
        record.assert_called_once()
        self.assertEqual(self.events.count("buy"), 1)
        self.assertIn("Arbitrage_Buy_Cleanup", [node.name for node in result.nodes])
        self.assertEqual(self.continued, 1)

    def test_purchase_action_failure_still_cleans_up_and_continues(self):
        self.purchase_failed = True
        result = self.run_native()
        self.assertEqual(self.events.count("buy"), 1)
        self.assertIn("Arbitrage_Buy_Cleanup", [node.name for node in result.nodes])
        self.assertEqual(self.continued, 1)

    def test_ready_failure_uses_pipeline_policy(self):
        with patch.object(buy.ArbitrageBuyReady, "run", return_value=False):
            self.run_native()
        self.assertEqual(self.events, ["scan", "mark", "buy"])
        self.assertEqual(self.continued, 1)

    def test_disabled_purchase_stage_cannot_enter_fallback(self):
        self.run_native(extra={"Arbitrage_BuyItem": {"enabled": False}})
        self.assertFalse(self.events)
        self.assertEqual(self.continued, 1)

    def test_purchase_confirmation_only_records_in_memory_evidence(self):
        self.assertTrue(buy.ArbitrageBuyListPrepare().run(self.context, self.argv))
        lists.get_purchase_run(99)["pending"].clear()
        self.assertTrue(buy.ArbitrageBuyComplete().run(self.context, self.argv))
        self.assertTrue(lists.completed_purchase_items(99, store.market_day()))
        self.assertEqual(self.writes, [])

    def test_old_alignment_timestamp_is_not_a_second_cycle_clock(self):
        records = {FIRST: {"items": sorted(TABLE[FIRST]), "name_version": lists.PURCHASE_NAME_VERSION,
                           "applied_at": "2000-01-01T00:00:00+00:00"}}
        self.assertEqual(lists.changed_cartridges({FIRST: TABLE[FIRST]}, records), [])
        self.assertEqual(lists.changed_cartridges({FIRST: set()}, records), [FIRST])

    def test_old_name_reader_is_rescanned_once_despite_current_week_mark(self):
        self.fresh_cycle()
        self.data["arbitrage"]["purchase_alignments"][FIRST].pop("name_version")
        self.run_native()
        self.assertEqual(self.scanned, [{FIRST}])
        records = store.get_purchase_alignments()
        self.assertEqual(records[FIRST]["name_version"], lists.PURCHASE_NAME_VERSION)
        self.assertEqual(lists.changed_cartridges(TABLE, records), [])

    def test_batch_invalidation_preserves_other_records_and_accounts_data(self):
        self.data["unrelated"] = "keep"
        self.assertTrue(store.invalidate_purchase_alignments([FIRST]))
        self.assertNotIn(FIRST, self.data["arbitrage"]["purchase_alignments"])
        self.assertEqual(len(self.data["arbitrage"]["purchase_alignments"]), len(TABLE) - 1)
        self.assertEqual(self.data["unrelated"], "keep")


if __name__ == "__main__":
    unittest.main()
