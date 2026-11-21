"""Private navigation identity, bounds, and frame-cache tests; no native Maa."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steal_test_helpers import load_action, recognition


PARAMS = {"card": 4, "destination": "shop", "expected_npc_names": ["商店老板"]}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_action("steal_navigation")
        self.context = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False),
            run_recognition=Mock(side_effect=lambda name, _: recognition(name, False)),
            run_action=Mock(return_value=SimpleNamespace(success=True)))
        self.nav = self.module.StealNavigator(self.context, PARAMS)

    def test_parameters_reject_wrong_card_destination_and_identity(self):
        bad = [None, {**PARAMS, "card": True}, {**PARAMS, "card": 0}, {**PARAMS, "card": 9},
               {**PARAMS, "destination": "anywhere"}, {**PARAMS, "expected_npc_names": []},
               {**PARAMS, "expected_npc_names": [" 商店老板"]}, {**PARAMS, "mark_complete": True}]
        for params in bad:
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.module.navigation_overrides(params)

    def test_board_requires_the_actual_board_name(self):
        for name in ("探索告示板", "任務布告欄", "探索佈告欄"):
            self.module.navigation_overrides({**PARAMS, "destination": "board", "expected_npc_names": [name]})
        with self.assertRaises(ValueError):
            self.module.navigation_overrides({**PARAMS, "destination": "board"})

    def test_identity_override_escapes_regex_metacharacters(self):
        params = {**PARAMS, "expected_npc_names": ["老板.*"]}
        overrides = self.module.navigation_overrides(params)
        self.assertEqual(overrides["StealNav_DestinationNpc"]["expected"], [r"^老板\.\*$"])

    def test_card_four_keeps_private_stable_art_and_other_thresholds(self):
        for number in range(1, 9):
            value = self.module.navigation_overrides({**PARAMS, "card": number})["StealNav_Card"]
            self.assertEqual(value["threshold"], .8 if number == 1 else .85)
            if number == 4:
                self.assertIn("Cartridges/Steal_sp4_active_art_PC.png", value["template"])

    def test_repeated_top_level_hit_and_miss_use_one_call(self):
        frame = object()
        self.assertFalse(self.nav.hit("Field", frame))
        self.assertFalse(self.nav.hit("Field", frame))
        self.context.run_recognition.assert_called_once_with("StealNav_Field", frame)

    def test_composite_children_are_reused_with_exact_result_objects(self):
        leaf = recognition("StealNav_StoryTab", box=[20, 30, 40, 20])
        parent = recognition("StealNav_QuickPanel", algorithm="And", children=[leaf])
        self.context.run_recognition.side_effect = None
        self.context.run_recognition.return_value = parent
        frame = object()
        self.nav.recognize("QuickPanel", frame)
        self.assertIs(self.nav.recognize("StoryTab", frame), leaf)
        self.assertEqual(self.context.run_recognition.call_count, 1)

    def test_failed_composite_reuses_only_executed_misses(self):
        miss = recognition("StealNav_StoryTab", False)
        parent = recognition("StealNav_QuickPanel", False, algorithm="And", children=[miss])
        self.context.run_recognition.side_effect = [parent, recognition("StealNav_RecentTab")]
        frame = object()
        self.assertFalse(self.nav.hit("QuickPanel", frame))
        self.assertFalse(self.nav.hit("StoryTab", frame))
        self.assertNotIn("RecentTab", self.nav._recognition_cache)
        self.assertTrue(self.nav.hit("RecentTab", frame))
        self.assertEqual(self.context.run_recognition.call_count, 2)

    def test_nested_short_circuit_results_do_not_invent_other_children(self):
        child = recognition("StealNav_Field", algorithm="Or", children=[recognition("StealNav_QuickButton")])
        parent = recognition("StealNav_ShopTarget", algorithm="Or", children=[child])
        self.context.run_recognition.side_effect = None
        self.context.run_recognition.return_value = parent
        frame = object()
        self.nav.recognize("ShopTarget", frame)
        self.assertTrue(self.nav.hit("QuickButton", frame))
        self.assertNotIn("ShopIcon", self.nav._recognition_cache)
        self.assertEqual(self.context.run_recognition.call_count, 1)

    def test_unknown_alias_and_incomplete_child_cannot_poison_cache(self):
        children = [recognition("OCR"), recognition("Global_Test"), recognition("StealNav_Unknown"),
                    recognition("StealNav_Card", hit=1), recognition("StealNav_GuideButton", reco_id=0),
                    SimpleNamespace(name="StealNav_StoryTab", hit=True, reco_id=1)]
        parent = recognition("StealNav_QuickPanel", algorithm="And", children=children)
        self.context.run_recognition.side_effect = None
        self.context.run_recognition.return_value = parent
        self.nav.recognize("QuickPanel", object())
        self.assertEqual(set(self.nav._recognition_cache), {"QuickPanel"})

    def test_new_frame_and_input_invalidate_cache(self):
        first, second = object(), object()
        self.nav.hit("Field", first)
        self.nav.hit("Field", second)
        self.nav.action("Click")
        self.nav.hit("Field", second)
        self.assertEqual(self.context.run_recognition.call_count, 3)

    def test_capture_invalidates_cache_even_if_controller_reuses_buffer(self):
        frame = SimpleNamespace(size=720 * 1280 * 3, shape=(720, 1280, 3))
        self.context.tasker.controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            done=True, succeeded=True, get=lambda: frame))
        self.nav.hit("Field", frame)
        self.nav.capture()
        self.nav.hit("Field", frame)
        self.assertEqual(self.context.run_recognition.call_count, 2)

    def test_unstarted_recognition_is_failure_and_not_cached(self):
        self.context.run_recognition.side_effect = None
        self.context.run_recognition.return_value = None
        with self.assertRaises(RuntimeError):
            self.nav.recognize("Field", object())
        self.assertNotIn("Field", self.nav._recognition_cache)

    def test_target_requires_unambiguous_exact_name_and_field_guards(self):
        def run(name, _):
            suffix = name.removeprefix("StealNav_")
            result = recognition(name, suffix in ("Field", "ShopTarget", "DestinationNpc"))
            result.filtered_results = [SimpleNamespace(text="商店老板")]
            return result
        self.context.run_recognition.side_effect = run
        self.assertTrue(self.nav.target_ready(object()))
        self.assertEqual(self.nav.confirmed_npc, "商店老板")
        def wrong(name, frame):
            result = run(name, frame)
            result.filtered_results = [SimpleNamespace(text="商店老板的任务")]
            return result
        self.context.run_recognition.side_effect = wrong
        self.assertFalse(self.nav.target_ready(object()))

    def test_preview_menu_cannot_be_treated_as_arrival(self):
        self.context.run_recognition.side_effect = lambda name, _: recognition(name, True)
        self.assertFalse(self.nav.target_ready(object()))

    def test_click_bounds_and_action_limit_prevent_input(self):
        with self.assertRaises(RuntimeError):
            self.nav.click(recognition("StealNav_Card", box=[1279, 10, 20, 20]))
        self.nav.actions = 24
        with self.assertRaises(RuntimeError):
            self.nav.action("Click")
        self.context.run_action.assert_not_called()

    def test_stop_prevents_capture_or_actions(self):
        self.context.tasker.stopping = True
        with self.assertRaises(RuntimeError):
            self.nav.capture()
        with self.assertRaises(RuntimeError):
            self.nav.action("Click")
        self.context.run_action.assert_not_called()

    def test_select_card_clicks_story_tab_coordinates_from_fresh_panel(self):
        old_panel = SimpleNamespace(tag="old")
        fresh_panel = SimpleNamespace(tag="fresh")
        card_frame = SimpleNamespace(tag="card")
        arrived_frame = SimpleNamespace(tag="arrived")
        frames = iter([fresh_panel, card_frame, arrived_frame, arrived_frame])
        self.nav.capture = lambda: next(frames)
        self.nav.pause = lambda _: None
        self.nav.loading = lambda _: False

        def reco(name, frame):
            suffix = name.removeprefix("StealNav_")
            if frame is old_panel:
                return recognition(name, suffix == "StoryTab", box=[300, 560, 70, 20] if suffix == "StoryTab" else None)
            if frame is fresh_panel:
                return recognition(name, suffix == "StoryTab", box=[310, 580, 70, 20] if suffix == "StoryTab" else None)
            if frame is card_frame:
                return recognition(name, suffix == "Card", box=[400, 620, 40, 40] if suffix == "Card" else None)
            return recognition(name, suffix == "Field")

        self.context.run_recognition.side_effect = reco
        result = self.nav.select_card(old_panel)
        self.assertIs(result, arrived_frame)
        first_click = self.context.run_action.call_args_list[0]
        self.assertEqual(first_click.args[0], "StealNav_Click")
        self.assertEqual(first_click.kwargs["pipeline_override"]["StealNav_Click"]["target"], [345, 590, 1, 1])

    def test_probe_on_card_closes_area_map_even_when_capture_raises(self):
        self.nav.capture = Mock(side_effect=RuntimeError("capture failed"))
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            self.nav.probe_on_card()
        called = [call.args[0] for call in self.context.run_action.call_args_list]
        self.assertEqual(called, ["StealNav_AreaMapOpen", "StealNav_AreaMapClose"])

    def test_probe_on_card_skips_close_when_stopping(self):
        def stop_on_capture():
            self.context.tasker.stopping = True
            raise RuntimeError("任务已停止")

        self.nav.capture = stop_on_capture
        with self.assertRaisesRegex(RuntimeError, "任务已停止"):
            self.nav.probe_on_card()
        called = [call.args[0] for call in self.context.run_action.call_args_list]
        self.assertEqual(called, ["StealNav_AreaMapOpen"])

    def test_open_quick_panel_recovers_leftover_steal_preview_and_npc_menu(self):
        f_preview = SimpleNamespace(hits={"PreviewDialog", "PreviewPresent", "PreviewCancel", "TalentHeading"})
        f_npc = SimpleNamespace(hits={"TalentHeading"})
        f_field = SimpleNamespace(hits={"Field", "QuickButton"})
        f_panel = SimpleNamespace(hits={"QuickPanel"})
        frames = iter([f_preview, f_npc, f_field, f_panel])
        self.nav.capture = lambda: next(frames)
        self.nav.pause = lambda _: None
        self.nav.loading = lambda _: False

        def reco(name, frame):
            suffix = name.removeprefix("StealNav_")
            hit = suffix in frame.hits
            box = None
            if hit and suffix == "PreviewCancel":
                box = [527, 442, 38, 25]
            elif hit and suffix == "QuickButton":
                box = [550, 650, 34, 34]
            return recognition(name, hit, box=box)

        self.context.run_recognition.side_effect = reco
        result = self.nav.open_quick_panel()
        self.assertIs(result, f_panel)
        actions = [(call.args[0], call.kwargs.get("pipeline_override")) for call in self.context.run_action.call_args_list]
        self.assertEqual(actions, [
            ("StealNav_Click", {"StealNav_Click": {"target": [546, 454, 1, 1]}}),
            ("StealNav_CloseHostInteraction", {}),
            ("StealNav_Click", {"StealNav_Click": {"target": [567, 667, 1, 1]}}),
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
