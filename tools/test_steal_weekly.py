"""Exercise weekly orchestration with fake tasks and an in-memory save view.

No Maa controller, application, input device or real account save is opened.
"""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
AGENT_ACTION = ROOT / "agent/action"
RESOURCES = ROOT / "assets/resource"
SOURCE = AGENT_ACTION / "steal_schedule.py"
package = ModuleType("steal_weekly_test_package")
package.__path__ = [str(AGENT_ACTION)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + ".steal_schedule", SOURCE)
schedule = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = schedule
spec.loader.exec_module(schedule)


def task_result(route, *, succeeded=True, mark=True, terminal=True):
    nodes = []
    if mark:
        nodes.append(SimpleNamespace(name=route.mark_node, completed=True))
    if terminal:
        nodes.append(SimpleNamespace(name=route.terminal, completed=True))
    return SimpleNamespace(status=SimpleNamespace(succeeded=succeeded), nodes=nodes)


class FakeContext:
    def __init__(self, harness):
        self.harness = harness
        self.tasker = harness.tasker

    def clone(self):
        child = FakeContext(self.harness)
        self.harness.clones.append(child)
        return child

    def run_task(self, entry, overrides):
        harness = self.harness
        harness.calls.append((entry, overrides, self))
        route = harness.by_entry[entry]
        behavior = harness.behaviors.get(entry, "success")
        if callable(behavior):
            return behavior(route)
        if behavior in ("topology", "final_scene_failed", "close_final_failed", "stop_before_final_scene"):
            # Exercise the actual JSON ordering rather than assuming that a
            # failed route has not already reached its persistence action.
            current = route.mark_node.removesuffix("_Mark") + "_Interact"
            executed = []
            for _ in range(12):
                if (behavior == "final_scene_failed" and current.endswith("_FinalScene")
                        or behavior == "close_final_failed" and current.endswith("_CloseFinal")):
                    return SimpleNamespace(status=SimpleNamespace(succeeded=False), nodes=executed)
                if behavior == "stop_before_final_scene" and current.endswith("_FinalScene"):
                    harness.tasker.stopping = True
                    return SimpleNamespace(status=SimpleNamespace(succeeded=False), nodes=executed)
                node = {**harness.pipeline[current], **overrides.get(current, {})}
                if node.get("custom_action") == "MarkComplete":
                    harness.marks.add(node["custom_action_param"]["card_name"])
                executed.append(SimpleNamespace(name=current, completed=True))
                targets = node.get("next", [])
                if not targets:
                    return SimpleNamespace(status=SimpleNamespace(succeeded=True), nodes=executed)
                if len(targets) != 1:
                    raise AssertionError("Expected one deterministic post-interaction edge")
                current = targets[0]
            raise AssertionError("Route tail did not terminate")
        if behavior == "exception":
            raise RuntimeError("simulated route error")
        if behavior == "failed":
            return task_result(route, succeeded=False)
        if behavior == "wrong_terminal":
            harness.marks.add(route.completion_key)
            return task_result(route, terminal=False)
        if behavior == "missing_mark":
            harness.marks.add(route.completion_key)
            return task_result(route, mark=False)
        if behavior != "not_persisted":
            harness.marks.add(route.completion_key)
        return task_result(route)


class Harness:
    def __init__(self, registry):
        self.tasker = SimpleNamespace(stopping=False)
        self.by_entry = {route.entry: route for route in registry.values() if route.entry}
        self.calls = []
        self.queries = []
        self.clones = []
        self.behaviors = {}
        self.marks = set()
        self.logger = Mock()
        self.context = FakeContext(self)
        self.pipeline = json.loads((RESOURCES / "base/pipeline/Steal_Routes.json").read_text(encoding="utf-8"))

    def check(self, route):
        self.queries.append(schedule.completion_params(route))
        return route.completion_key in self.marks

    def run(self, routes, check=None, *, skip_completed=True):
        return schedule.WeeklyRunner(self.context, check or self.check, self.logger).run(
            routes, skip_completed=skip_completed)


class WeeklySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.registry = schedule.load_registry()
        self.first = self.registry["C1_Collector"]
        self.second = self.registry["C3_Shopkeeper"]
        self.harness = Harness(self.registry)

    def test_registry_has_fixed_thirteen_ids_and_independent_completion_keys(self):
        self.assertEqual(set(self.registry), {
            "C1_CuriousYouth", "C1_Collector", "C2_LoanShark",
            "C3_Mercenary", "C3_Shopkeeper", "C4_CoolWindYouth", "C4_TavernGuest",
            "C5_Merchant", "C5_Lover", "C6_Apprentice", "C6_Stone",
            "C7_Boatman", "C8_TaxCollector",
        })
        self.assertEqual(len({route.completion_key for route in self.registry.values()}), 13)
        for route in self.registry.values():
            with self.subTest(route=route.route_id):
                self.assertTrue(route.completion_key.endswith("_3Characters"))
                self.assertIs(type(route.live_verified), bool)
                nodes = (route.entry, route.terminal, route.mark_node)
                if route.entry is None:
                    self.assertEqual(nodes, (None, None, None))
                    self.assertFalse(route.live_verified)
                else:
                    self.assertTrue(all(isinstance(node, str) and node for node in nodes))
                    self.assertEqual(len(set(nodes)), 3)

    def test_available_entries_and_marks_match_the_route_pipeline(self):
        nodes = json.loads((RESOURCES / "base/pipeline/Steal_Routes.json").read_text("utf-8"))
        for route in self.registry.values():
            if not route.entry:
                continue
            with self.subTest(route=route.route_id):
                self.assertIn(route.entry, nodes)
                self.assertIn(route.terminal, nodes)
                self.assertEqual(nodes[route.mark_node]["custom_action"], "MarkComplete")
                self.assertEqual(nodes[route.mark_node]["custom_action_param"], {
                    "card_name": route.completion_key, "cycle_type": "steal_weekly"})

    def test_every_route_verifies_final_scene_before_persistence_and_new_terminal(self):
        nodes = self.harness.pipeline
        for route in self.registry.values():
            prefix = route.mark_node.removesuffix("_Mark")
            with self.subTest(route=route.route_id):
                self.assertEqual(nodes[prefix + "_Interact"]["next"], [prefix + "_CloseFinal"])
                self.assertEqual(nodes[prefix + "_CloseFinal"]["next"], [prefix + "_FinalScene"])
                self.assertEqual(nodes[prefix + "_FinalScene"]["next"], [route.mark_node])
                self.assertEqual(nodes[prefix + "_FinalScene"]["on_error"], ["Steal_RouteFail"])
                self.assertEqual(nodes[route.mark_node]["next"], [route.terminal])
                self.assertEqual(route.terminal, prefix + "_Complete")
                self.assertEqual(nodes[route.terminal]["recognition"], "DirectHit")
                self.assertEqual(nodes[route.terminal]["on_error"], [])
                self.assertNotIn("custom_action", nodes[route.terminal])

    def test_final_scene_failure_writes_no_record_and_next_run_retries_all_thirteen_routes(self):
        for route in self.registry.values():
            with self.subTest(route=route.route_id):
                harness = Harness(self.registry)
                harness.behaviors[route.entry] = "final_scene_failed"
                first = harness.run((route,))
                self.assertEqual(first.outcomes[0].status, "failed")
                self.assertNotIn(route.completion_key, harness.marks)
                harness.behaviors[route.entry] = "topology"
                second = harness.run((route,))
                self.assertEqual(second.outcomes[0].status, "completed")
                self.assertEqual(len(harness.calls), 2)
                third = harness.run((route,))
                self.assertEqual(third.outcomes[0].status, "skipped")
                self.assertEqual(len(harness.calls), 2)

    def test_close_final_failure_cannot_persist_or_skip_retry(self):
        self.harness.behaviors[self.first.entry] = "close_final_failed"
        self.assertEqual(self.harness.run((self.first,)).outcomes[0].status, "failed")
        self.assertNotIn(self.first.completion_key, self.harness.marks)
        self.harness.behaviors[self.first.entry] = "topology"
        self.assertEqual(self.harness.run((self.first,)).outcomes[0].status, "completed")
        self.assertEqual(len(self.harness.calls), 2)

    def test_stop_before_final_scene_has_no_persistence(self):
        self.harness.behaviors[self.first.entry] = "stop_before_final_scene"
        first = self.harness.run((self.first,))
        self.assertTrue(first.stopped)
        self.assertNotIn(self.first.completion_key, self.harness.marks)
        self.harness.tasker.stopping = False
        self.harness.behaviors[self.first.entry] = "topology"
        self.assertEqual(self.harness.run((self.first,)).outcomes[0].status, "completed")
        self.assertEqual(len(self.harness.calls), 2)

    def test_one_failed_route_does_not_prevent_next_and_overall_is_false(self):
        self.harness.behaviors[self.first.entry] = "failed"
        report = self.harness.run((self.first, self.second))
        self.assertFalse(report.succeeded)
        self.assertEqual([item.status for item in report.outcomes], ["failed", "completed"])
        self.assertEqual([call[0] for call in self.harness.calls], [self.first.entry, self.second.entry])
        self.assertNotIn(self.first.completion_key, self.harness.marks)
        self.assertIn(self.second.completion_key, self.harness.marks)

    def test_route_exception_still_allows_next_independent_route(self):
        self.harness.behaviors[self.first.entry] = "exception"
        report = self.harness.run((self.first, self.second))
        self.assertFalse(report.succeeded)
        self.assertEqual(len(self.harness.calls), 2)
        self.assertIn("simulated route error", report.outcomes[0].reason)
        self.assertEqual(report.outcomes[1].status, "completed")

    def test_terminal_override_prevents_calibration_chain_from_running_twice(self):
        report = self.harness.run((self.first, self.second))
        self.assertTrue(report.succeeded)
        for route, call in zip((self.first, self.second), self.harness.calls):
            self.assertEqual(call[1], {route.terminal: {"next": [], "on_error": []}})
        self.assertIsNot(self.harness.calls[0][2], self.harness.calls[1][2])

    def test_completed_three_character_key_skips_without_input_task(self):
        self.harness.marks.add(self.first.completion_key)
        report = self.harness.run((self.first, self.second))
        self.assertTrue(report.succeeded)
        self.assertEqual([item.status for item in report.outcomes], ["skipped", "completed"])
        self.assertEqual([call[0] for call in self.harness.calls], [self.second.entry])
        self.assertEqual(self.harness.queries[0], {
            "targets": [{"card_name": "Steal_C1_Collector_3Characters", "cycle_type": "steal_weekly"}],
            "match": "all_done"})

    def test_old_single_character_c2_mark_is_not_reused(self):
        self.harness.marks.add("Steal_C2_LoanShark")
        route = self.registry["C2_LoanShark"]
        self.assertEqual(schedule.completion_params(route)["targets"][0]["card_name"],
                         "Steal_C2_LoanShark_3Characters")
        report = self.harness.run((route,))
        self.assertTrue(report.succeeded)
        self.assertEqual(report.outcomes[0].status, "completed")
        self.assertEqual(len(self.harness.calls), 1)
        self.assertTrue(all(query["targets"][0]["card_name"] == "Steal_C2_LoanShark_3Characters"
                            for query in self.harness.queries))

    def test_disabling_weekly_skip_rechecks_completed_routes(self):
        self.harness.marks.update((self.first.completion_key, self.second.completion_key))
        routes = schedule.select_routes({"route_ids": [self.first.route_id, self.second.route_id],
                                         "skip_completed": False}, self.registry)
        report = self.harness.run(routes, skip_completed=False)
        self.assertTrue(report.succeeded)
        self.assertEqual([o.status for o in report.outcomes], ["completed", "completed"])
        self.assertEqual([c[0] for c in self.harness.calls], [self.first.entry, self.second.entry])
        # Only the post-route persistence checks run when skipping is disabled.
        self.assertEqual(len(self.harness.queries), 2)

    def test_recheck_does_not_accept_old_mark_after_route_failure(self):
        self.harness.marks.update((self.first.completion_key, self.second.completion_key))
        self.harness.behaviors[self.first.entry] = "wrong_terminal"
        report = self.harness.run((self.first, self.second), skip_completed=False)
        self.assertFalse(report.succeeded)
        self.assertEqual([o.status for o in report.outcomes], ["failed", "completed"])
        self.assertEqual(len(self.harness.calls), 2)

    def test_skip_option_rejects_non_boolean_values(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                schedule.select_routes({"skip_completed": value}, self.registry)
            with self.subTest(runner=value), self.assertRaises(ValueError):
                self.harness.run((self.first,), skip_completed=value)

    def test_task_switch_controls_both_entry_points_and_defaults_to_skip(self):
        interface = json.loads((ROOT / "assets/interface.json").read_text("utf-8-sig"))
        key = "偷窃-跳过本周完成记录"
        option = interface["option"][key]
        self.assertEqual(option["type"], "switch")
        self.assertEqual(option["default_case"], "Yes")
        tasks = [task for task in interface["task"] if task.get("entry") in ("Steal_Start", "Steal_Start2")]
        self.assertEqual(sorted(task["entry"] for task in tasks), ["Steal_Start", "Steal_Start2"])
        for task in tasks:
            self.assertIn(key, task["option"])
            self.assertIs(task.get("default_check", False), False)
        self.assertEqual({case["name"] for case in option["cases"]}, {"Yes", "No"})
        for case in option["cases"]:
            expected = case["name"] == "Yes"
            for entry in ("Steal_Start", "Steal_Start2"):
                self.assertIs(case["pipeline_override"][entry]["custom_action_param"]["skip_completed"], expected)

    def test_missing_route_cannot_become_success_from_a_mark_alone(self):
        # Keep the missing-route behavior test even after every real route exists.
        route = replace(self.first, entry=None, terminal=None, mark_node=None, live_verified=False)
        self.harness.marks.add(route.completion_key)
        report = self.harness.run((route,))
        self.assertFalse(report.succeeded)
        self.assertEqual(report.outcomes[0].status, "pending")
        self.assertEqual(self.harness.calls, [])
        self.assertEqual(self.harness.queries, [])

    def test_native_success_without_terminal_or_mark_is_not_completion(self):
        for behavior in ("wrong_terminal", "missing_mark", "not_persisted"):
            with self.subTest(behavior=behavior):
                harness = Harness(self.registry)
                harness.behaviors[self.first.entry] = behavior
                report = harness.run((self.first,))
                self.assertFalse(report.succeeded)
                self.assertEqual(report.outcomes[0].status, "failed")

    def test_stop_before_run_has_no_save_reads_or_route_calls(self):
        self.harness.tasker.stopping = True
        report = self.harness.run((self.first, self.second))
        self.assertTrue(report.stopped)
        self.assertFalse(report.succeeded)
        self.assertEqual(self.harness.queries, [])
        self.assertEqual(self.harness.calls, [])
        self.assertEqual([item.status for item in report.outcomes], ["not_run", "not_run"])

    def test_stop_during_completion_check_never_launches_route(self):
        def stopped_check(route):
            self.harness.tasker.stopping = True
            return False
        report = self.harness.run((self.first, self.second), check=stopped_check)
        self.assertTrue(report.stopped)
        self.assertFalse(report.succeeded)
        self.assertEqual(self.harness.calls, [])

    def test_stop_during_first_route_does_not_read_or_run_later_route(self):
        def stop_inside(route):
            self.harness.tasker.stopping = True
            return task_result(route)
        self.harness.behaviors[self.first.entry] = stop_inside
        report = self.harness.run((self.first, self.second))
        self.assertTrue(report.stopped)
        self.assertFalse(report.succeeded)
        self.assertEqual(len(self.harness.calls), 1)
        self.assertEqual(len(self.harness.queries), 1)
        self.assertEqual([item.status for item in report.outcomes], ["interrupted", "not_run"])

    def test_selection_rejects_unknown_duplicate_empty_or_extra_parameters(self):
        bad = ({"route_ids": ["C99_Invalid"]}, {"route_ids": []},
               {"route_ids": ["C1_Collector", "C1_Collector"]},
               {"route_ids": "C1_Collector"}, {"force": True}, None)
        for params in bad:
            with self.subTest(params=params), self.assertRaises(ValueError):
                schedule.select_routes(params, self.registry)

    def test_default_run_keeps_unimplemented_targets_unfinished(self):
        # Deliberately remove a route in this fixture; implementation progress
        # must not make this negative-path test disappear or change its meaning.
        registry = dict(self.registry)
        registry[self.first.route_id] = replace(
            self.first, entry=None, terminal=None, mark_node=None, live_verified=False)
        routes = schedule.select_routes({}, registry)
        expected_pending = {route.route_id for route in routes if route.entry is None}
        report = self.harness.run(routes)
        self.assertFalse(report.succeeded)
        self.assertEqual(len(report.outcomes), 13)
        self.assertEqual({item.route_id for item in report.unfinished}, expected_pending)
        self.assertTrue(all(item.status == ("pending" if item.route_id in expected_pending else "completed")
                            for item in report.outcomes))
        schedule.log_report(report, self.harness.logger)
        messages = [str(call.args[0]) for call in self.harness.logger.error.call_args_list]
        self.assertTrue(any(f"未完成{len(expected_pending)}" in message for message in messages))
        self.assertTrue(any(self.first.name in message and "未完成" in message for message in messages))

    def test_registry_rejects_reusing_old_single_character_completion_key(self):
        payload = json.loads(schedule.REGISTRY_PATH.read_text("utf-8"))
        next(route for route in payload["routes"] if route["route_id"] == "C2_LoanShark")["completion_key"] = "Steal_C2_LoanShark"
        with tempfile.TemporaryDirectory(prefix="steal_registry_test_") as folder:
            path = Path(folder) / "registry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "三个角色"):
                schedule.load_registry(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
