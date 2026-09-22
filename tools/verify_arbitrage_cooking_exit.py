"""常规/补做入口与出口锚点回归；真实资源合并与内核，空白控制器，不连接游戏。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from maa.custom_action import CustomAction
from maa.define import LoggingLevelEnum
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

Library.version()
from maa.agent.agent_server import AgentServer
# Native tests register callbacks on Resource, not on an AgentServer process.
with patch.object(AgentServer, "_set_api_properties"), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls):
    from verify_arbitrage_purchase_cycle import OfflineController, PatchPipeline
from action import arbitrage_replenish_cook as cook
from utils.arbitrage_recipe_catalog import (
    COOKING_EXIT_ANCHOR, discover_recipe_entries, build_replenish_selection,
)


ROOT = Path(__file__).resolve().parents[1]
NORMAL = "Arbitrage_Cooking_Run"
REPLENISH = "Arbitrage_Cooking_Replenish_Run"
PREPARE = "Arbitrage_Cooking_MenuPatch"
FIRST = "Arbitrage_Cooking_MenuEnter"
PAGE = "Arbitrage_Cooking_Page1"
PROTECTION = "Arbitrage_Cooking_MateProtec"
NORMAL_END = "Arbitrage_Cooking_ERREND"
DAY = "2026-09-21"


class Recorder(CustomAction):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def run(self, context, argv):
        self.events.append(argv.node_name)
        return True


class Runner(CustomAction):
    def __init__(self, owner, targets, pc, events):
        super().__init__()
        self.owner, self.targets, self.pc, self.events = owner, targets, pc, events
        self.failure = None

    def run(self, context, argv):
        try:
            check = self.owner
            entries = discover_recipe_entries(context)
            all_selected = build_replenish_selection(entries, [row.name for row in entries if row.enabled])
            watched = (FIRST, PAGE, PROTECTION, NORMAL_END, NORMAL, REPLENISH)
            original = {name: context.get_node_data(name) for name in watched}
            # A single task alternates both callers, so shared counters and anchor
            # residue cannot be hidden by starting each case in a fresh task.
            for _ in range(2):
                local = context.clone()
                for name in (*all_selected.clear_hit_nodes, NORMAL, PROTECTION):
                    check.assertTrue(local.clear_hit_count(name))
                detail = local.run_task(NORMAL)
                check.assertTrue(detail.status.succeeded)
                normal_names = {node.name for node in detail.nodes}
                check.assertIn(NORMAL_END, normal_names)
                check.assertNotIn(cook.REPLENISH_END, normal_names)
                check.assertEqual(PAGE in normal_names, not self.pc)
                check.assertFalse(context.get_anchor(COOKING_EXIT_ANCHOR))
                before_events = list(self.events)
                with patch.object(cook, "sync_from_context", return_value=True), \
                     patch.object(cook.store, "market_day", return_value=DAY), \
                     patch.object(cook.store, "get_replenish_inventory", return_value={}), \
                     patch.object(cook, "get_cooking_stock", return_value=[]):
                    result = cook.execute_replenish_cooking(context, self.targets, day=DAY,
                                                           bag_run_id="memory", callback_task_id=argv.task_detail.task_id)
                check.assertEqual(result["status"], "completed", result)
                check.assertTrue(result["queue_completed"])
                check.assertEqual(set(result["visited"]), set(self.targets))
                check.assertEqual(result["selected"], [])
                check.assertEqual(self.events, before_events)  # no normal finish or repeated protection
                check.assertFalse(context.get_anchor(COOKING_EXIT_ANCHOR))
                for name, before in original.items():
                    check.assertEqual(context.get_node_data(name), before, name)
            return True
        except Exception as exc:
            self.failure = exc
            return False


class CookingExitTests(unittest.TestCase):
    def test_base_and_pc_keep_routes_and_alternate_exit_owners(self):
        # MaaFramework keeps its log handle open until process exit on Windows.
        with tempfile.TemporaryDirectory(prefix="mfabd2-cooking-exit-", ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(directory)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            try:
                for pc in (False, True):
                    for targets in (["香草牛排"], ["香草牛排", "甜辣鲜虾"]):
                        with self.subTest(pc=pc, targets=targets):
                            self.run_case(pc, targets)
            finally:
                Tasker.set_log_dir("")

    def run_case(self, pc, targets):
        if pc and not (ROOT / "assets/resource/pc/pipeline/Arbitrage.json").is_file():
            self.skipTest("PC arbitrage overlay is not present on this branch")
        resource = Resource()
        self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
        if pc:
            self.assertTrue(resource.post_bundle(ROOT / "assets/resource/pc").wait().succeeded)
        normal = resource.get_node_data(NORMAL)
        replen = resource.get_node_data(REPLENISH)
        self.assertEqual(normal["next"], replen["next"])
        self.assertEqual(normal["anchor"][COOKING_EXIT_ANCHOR], NORMAL_END)
        self.assertEqual(replen["anchor"][COOKING_EXIT_ANCHOR], cook.REPLENISH_END)
        entries = discover_recipe_entries(resource)  # exit anchor is intentionally unset
        selection = build_replenish_selection(entries, targets)
        self.assertTrue(all(set(value) == {"enabled"} for value in selection.pipeline_override.values()))
        menu = resource.get_node_data(FIRST)
        self.assertEqual(PAGE in [link["name"] for link in menu["next"]], not pc)
        consumers = menu["next"] if pc else resource.get_node_data(PAGE)["on_error"]
        self.assertTrue(any(link["name"] == COOKING_EXIT_ANCHOR and link.get("anchor") for link in consumers))
        fixture = {
            PREPARE: {"action": "DoNothing", "next": [FIRST]},
            FIRST: {"recognition": "DirectHit"},
            PROTECTION: {"recognition": "DirectHit", "action": "Custom", "custom_action": "test_record", "next": []},
            "Arbitrage_Cooking_Err_At_Sandbox": {"enabled": False},
            "Arbitrage_Cooking_Menu_Reset": {"enabled": False},
            "Arbitrage_Cooking_Menu_Residue": {"enabled": False},
            "Arbitrage_Cooking_Swip_Page2": {"enabled": False},
            "Arbitrage_Cooking_Menu_ReBack": {"recognition": "DirectHit"},
            cook._MENU: {"recognition": "DirectHit"},
            "test_cooking_exit": {"action": "Custom", "custom_action": "test_cooking_exit"},
        }
        for row in entries:
            fixture[row.entry] = {"enabled": row.name in {"香草牛排", "甜辣鲜虾", "冰镇甜点"}}
            for selector in row.selectors:
                # Missing material: the entry is visited but no selector succeeds.
                # Keep the template-based recipe identity for catalog discovery.
                # The blank controller cannot match these real recipe images.
                fixture[selector] = {"enabled": row.name in {"香草牛排", "甜辣鲜虾", "冰镇甜点"}}
        for name in {*fixture, PAGE, NORMAL, NORMAL_END, REPLENISH, cook.REPLENISH_END}:
            fixture.setdefault(name, {}).update(pre_delay=0, post_delay=0, rate_limit=0, timeout=1)
        self.assertTrue(resource.override_pipeline(fixture))
        events = []
        runner = Runner(self, targets, pc, events)
        self.assertTrue(resource.register_custom_action("test_cooking_exit", runner))
        self.assertTrue(resource.register_custom_action("test_record", Recorder(events)))
        self.assertTrue(resource.register_custom_action("ArbitrageCookingFinish", Recorder(events)))
        self.assertTrue(resource.register_custom_action("PatchPipeline", PatchPipeline()))
        controller = OfflineController()
        self.assertTrue(controller.post_connection().wait().succeeded)
        tasker = Tasker()
        self.assertTrue(tasker.bind(resource, controller))
        try:
            detail = tasker.post_task("test_cooking_exit").wait().get()
            if runner.failure:
                raise runner.failure
            self.assertTrue(detail.status.succeeded)
            self.assertEqual(events.count(NORMAL_END), 2)
            self.assertEqual(events.count(PROTECTION), 2)
        finally:
            tasker.post_stop().wait()
            del tasker
            del controller
            resource.clear()


if __name__ == "__main__":
    unittest.main()
