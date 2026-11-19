"""零制作时的仓检/补买候选回归；使用内存状态和空白控制器，不连接游戏。"""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer
with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls):
    from action import arbitrage_flow as flow
from utils.arbitrage_recipe_catalog import RecipeEntry
from utils.persistent_store import PersistentStore
from verify_arbitrage_purchase_cycle import OfflineController


PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
SELECTOR = next(name for name, node in PIPE.items()
                if any("Shop/RecipeList/料理_香草牛排.png" in child.get("template", [])
                       for child in node.get("all_of", []) if isinstance(child, dict)))
ENTRY = SELECTOR + "_Entry"
RECIPE = RecipeEntry(ENTRY, (SELECTOR,), "香草牛排", True, (ENTRY,))
MATERIALS = {"兽肉", "香草", "盐", "黄油", "胡椒"}


def at(clock):
    return f"2026-10-28T{clock}:00+00:00"


def moment(clock):
    return datetime.fromisoformat(at(clock))


def known(quantity, clock):
    return {"quantity": quantity, "quantity_status": "known", "quantity_observed_at": at(clock),
            "quantity_source": "bag_detail"}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.dict(flow._COOKING_RUNS, {99: "999"}, clear=True),
                  patch.dict(flow._NORMAL_COMPLETED, clear=True),
                  patch.object(PersistentStore, "_current_account_id", "999"),
                  patch.object(flow, "sync_from_context", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.overrides = []
        self.context = NS(get_node_data=lambda _: {"enabled": True},
                          get_node_object=lambda _: NS(attach={"limit_budget": False}),
                          override_pipeline=lambda value: self.overrides.append(value) is None)

    def finish(self, names=(ENTRY,)):
        # 入口的后继识别可以失败，且没有任何选择/制作动作成功。
        nodes = [NS(name=name, completed=False, action=None) for name in names]
        argv = NS(node_name="Arbitrage_Cooking_ERREND", task_detail=NS(task_id=99, nodes=nodes))
        self.assertTrue(flow.ArbitrageCookingFinish().run(self.context, argv))

    def entries(self, recipes=(RECIPE,), task_id=99):
        with patch.object(flow, "discover_recipe_entries", return_value=recipes):
            return flow.replenishment_entries(self.context, task_id)

    def test_failed_selection_still_allows_visited_recipe(self):
        self.finish()
        self.assertTrue(self.entries()[0].enabled)
        self.assertEqual(flow._NORMAL_COMPLETED[99]["visited_entries"], {ENTRY})

    def test_successful_selector_is_not_a_substitute_for_visited_entry(self):
        self.finish((SELECTOR,))
        self.assertFalse(self.entries()[0].enabled)

    def test_user_disabled_or_material_protected_recipe_stays_disabled(self):
        self.finish()
        disabled = RecipeEntry(ENTRY, (SELECTOR,), "香草牛排", False, (ENTRY,))
        self.assertFalse(self.entries((disabled,))[0].enabled)
        self.assertFalse(disabled.enabled)

    def test_unvisited_recipe_is_not_added(self):
        self.finish()
        other = RecipeEntry("Arbitrage_Cooking_A2_Entry", ("Arbitrage_Cooking_A2",), "蜂蜜黄油杏仁", True, ())
        result = self.entries((RECIPE, other))
        self.assertEqual([row.enabled for row in result], [True, False])

    def test_missing_finish_other_task_and_other_account_cannot_reuse_scope(self):
        self.assertEqual(self.entries(), [])
        self.finish()
        self.assertEqual(self.entries(task_id=100), [])
        with patch.object(PersistentStore, "_current_account_id", "other"):
            self.assertEqual(self.entries(), [])

    def test_zero_successful_cooking_requests_bag_materials(self):
        self.finish()
        materials = {"兽肉", "香草", "盐", "黄油", "胡椒"}
        scan = {"executed": True, "returned": True, "scan_id": "s1", "pages": 3,
                "quantities": {"兽肉": 12, "盐": 5}, "absent": ["香草"], "unknown": {"胡椒": "detail_unreadable"}}
        with patch.object(flow, "discover_recipe_entries", return_value=(RECIPE,)), \
             patch.object(flow, "bag_catalog", return_value=dict.fromkeys(materials - {"黄油"})), \
             patch.object(flow.store, "get_inventory_items", return_value={}), \
             patch.object(flow.store, "invalidate_inventory_quantities", return_value=True) as invalidate, \
             patch.object(flow.store, "save_bag_stock_summary", return_value=True) as save, \
             patch.object(flow, "get_cooking_scan_start", return_value=None), \
             patch.object(flow, "trust_since", return_value=None), \
             patch.object(flow, "run_bag_scan", return_value=scan) as run_scan, \
             patch.object(flow.mfaalog, "info") as log:
            argv = NS(node_name="Arbitrage_BagStockScan", task_detail=NS(task_id=99))
            self.assertTrue(flow.ArbitrageBagScan().run(self.context, argv))
        # 缺模板的黄油不进背包，但同样作废旧量、记为未知。
        invalidate.assert_called_once_with(sorted(materials), "bag_scan_pending")
        run_scan.assert_called_once_with(self.context, label="套利料理后补查",
                                         targets=sorted(materials - {"黄油"}))
        summary = save.call_args.args[0]
        self.assertEqual(summary["read_item_names"], ["兽肉", "盐"])
        self.assertEqual(summary["absent_item_names"], ["香草"])
        self.assertEqual(summary["unknown_item_names"], ["胡椒", "黄油"])
        self.assertEqual(summary["errors"], {"黄油": "template_missing", "胡椒": "detail_unreadable"})
        self.assertFalse(summary["complete"])
        from action.bag_stock import get_bag_scan_run
        self.assertEqual(get_bag_scan_run(99), summary["run_id"])
        self.assertIn("配方需要5项", log.call_args_list[0].args[0])

    def bag_check(self, items, *, since=None, cooking_start=None):
        """跑一次 ③ 补查（香草牛排 5 样材料都有图标），返回（进背包查的、作废的、汇总）。"""
        self.finish()
        trust = {"side_effect": since} if isinstance(since, Exception) else {"return_value": since}
        scan = {"executed": True, "returned": True, "scan_id": "s1", "pages": 1, "quantities": {}, "absent": [],
                "unknown": {}}
        with patch.object(flow, "discover_recipe_entries", return_value=(RECIPE,)), \
             patch.object(flow, "bag_catalog", return_value=dict.fromkeys(MATERIALS)), \
             patch.object(flow.store, "get_inventory_items", return_value=items), \
             patch.object(flow.store, "invalidate_inventory_quantities", return_value=True) as invalidate, \
             patch.object(flow.store, "save_bag_stock_summary", return_value=True) as save, \
             patch.object(flow, "get_cooking_scan_start", return_value=cooking_start), \
             patch.object(flow, "trust_since", **trust), \
             patch.object(flow, "run_bag_scan", return_value=scan) as run_scan, \
             patch.object(flow.mfaalog, "warning"):
            argv = NS(node_name="Arbitrage_BagStockScan", task_detail=NS(task_id=99))
            self.assertTrue(flow.ArbitrageBagScan().run(self.context, argv))
        scanned = run_scan.call_args.kwargs["targets"] if run_scan.called else []
        return scanned, invalidate.call_args.args[0], save.call_args.args[0]

    def test_nothing_to_scan_still_records_reuse_without_entering_bag(self):
        items = {name: known(3, "01:00") for name in MATERIALS}
        scanned, _, summary = self.bag_check(items, cooking_start=at("00:00"))
        self.assertEqual(scanned, [])
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["reused_item_names"], sorted(MATERIALS))

    def test_readings_since_the_ledger_scan_are_reused_when_nothing_was_cooked(self):
        """试跑10 第 2、3 轮：没做菜，开头那盘读的数都还准，不再全部进背包。"""
        items = {name: known(3, "00:30") for name in MATERIALS}
        scanned, pending, _ = self.bag_check(items, since=moment("00:10"), cooking_start=at("01:00"))
        self.assertEqual((scanned, pending), ([], []))

    def test_readings_before_the_ledger_scan_or_invalidated_are_checked(self):
        items = {name: known(3, "00:30") for name in MATERIALS}
        items["兽肉"] = known(3, "00:05")                                                   # 盘点之前
        items["盐"] = {"quantity_status": "unknown", "quantity_source": "invalidated:cooking_attempt"}
        items["黄油"] = {"quantity_status": "unknown", "quantity_source": "invalidated:purchase_bought"}
        items["胡椒"] = dict(known(3, "00:30"), quantity_observed_at="2026-10-28T00:30:00")   # 没时区，不猜
        scanned, _, _ = self.bag_check(items, since=moment("00:10"), cooking_start=at("01:00"))
        self.assertEqual(scanned, sorted({"兽肉", "盐", "黄油", "胡椒"}))

    def test_without_ledger_floor_falls_back_to_cooking_start(self):
        items = {name: known(3, "00:30") for name in MATERIALS}
        items["香草"] = known(3, "01:30")
        for since in (None, RuntimeError("事件读不到")):
            with self.subTest(since=since):
                scanned, _, _ = self.bag_check(items, since=since, cooking_start=at("01:00"))
                self.assertEqual(scanned, sorted(MATERIALS - {"香草"}))

    def test_new_cooking_attempt_clears_old_completion_even_for_empty_selection(self):
        self.finish()
        with patch.object(flow, "discover_recipe_entries", return_value=()):
            self.assertTrue(flow.ArbitrageCookingPrepare().run(self.context, NS(task_detail=NS(task_id=99))))
        self.assertNotIn(99, flow._NORMAL_COMPLETED)
        self.assertNotIn(99, flow._COOKING_RUNS)


class NativeCandidateTests(unittest.TestCase):
    def test_entry_with_no_matching_selector_remains_candidate_in_real_task_detail(self):
        with tempfile.TemporaryDirectory(prefix="mfabd2-cooking-candidates-") as directory, \
             patch.dict(flow._COOKING_RUNS, clear=True), patch.dict(flow._NORMAL_COMPLETED, clear=True), \
             patch.object(PersistentStore, "_current_account_id", "999"), \
             patch.object(flow, "sync_from_context", return_value=True):
            self.assertTrue(Toolkit.init_option(directory, {"logging": False}))
            self.assertTrue(Tasker.set_save_on_error(False))
            resource = Resource()
            self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
            self.assertTrue(resource.register_custom_action("ArbitrageCookingPrepare", flow.ArbitrageCookingPrepare()))
            self.assertTrue(resource.register_custom_action("ArbitrageCookingFinish", flow.ArbitrageCookingFinish()))
            overrides = {name: {"enabled": False} for name in PIPE
                         if name.startswith("Arbitrage_Cooking_") and name.endswith("_Entry")}
            selector = deepcopy(PIPE[SELECTOR])
            selector["enabled"] = True
            selector["all_of"][0]["threshold"] = 1.0
            overrides.update({
                "Arbitrage_Cooking": {"next": ["[JumpBack]" + ENTRY, "Arbitrage_Cooking_ERREND"]},
                ENTRY: {"enabled": True, "next": [SELECTOR], "on_error": ["Global_Null"],
                        "timeout": 1, "rate_limit": 0, "pre_delay": 0, "post_delay": 0},
                SELECTOR: selector,
            })
            self.assertTrue(resource.override_pipeline(overrides))
            controller = OfflineController()
            self.assertTrue(controller.post_connection().wait().succeeded)
            tasker = Tasker()
            tasker.bind(resource, controller)
            detail = tasker.post_task("Arbitrage_Cooking").wait().get()
            self.assertTrue(detail.status.succeeded)
            self.assertFalse(any(node.name == SELECTOR for node in detail.nodes))
            self.assertIn(ENTRY, flow._NORMAL_COMPLETED[detail.task_id]["visited_entries"])
            entries = flow.replenishment_entries(resource, detail.task_id)
            self.assertEqual([row.name for row in entries if row.enabled], ["香草牛排"])
            tasker.post_stop().wait()
            del tasker
            del controller
            resource.clear()


if __name__ == "__main__":
    unittest.main()
