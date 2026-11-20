"""Interaction authorization and completion regressions without screenshots."""

import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steal_test_helpers import BASE, load_action


def task(names, success=True):
    return SimpleNamespace(status=SimpleNamespace(succeeded=success),
                           nodes=[SimpleNamespace(name="StealInteraction_" + name) for name in names])


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.module = load_action("steal_interaction")
        self.nodes = json.loads((BASE / "pipeline/Steal_Interaction.json").read_text(encoding="utf-8"))

    def test_both_layouts_keep_bargaining_and_upgrade_buttons_out_of_targets(self):
        for offset, xs in ((0, (126, 209, 291)), (1, (209, 291, 373))):
            overrides = self.module.card_layout_overrides(offset)
            for number, x in enumerate(xs, 1):
                self.assertEqual(overrides[f"StealInteraction_Card{number}Select"]["target"], [x, 610, 1, 1])
                self.assertLess(610, 650)
                if offset:
                    self.assertGreater(x, 165)
        for bad in (-1, 2, True, "1"):
            with self.assertRaises(ValueError):
                self.module.card_layout_overrides(bad)

    def test_countdown_branches_keep_strict_crops_patterns_and_threshold(self):
        branches = self.module.cooldown_conditions(88)
        self.assertEqual([branch["roi"] for branch in branches], [[96, 593, 62, 23], [96, 590, 62, 30]])
        self.assertEqual([branch["only_rec"] for branch in branches], [True, False])
        self.assertEqual([branch["threshold"] for branch in branches], [.7, .7])
        patterns = [branch["expected"][0] for branch in branches]
        for text in ("0:00", "167:59", "5天", "7 天"):
            self.assertTrue(any(re.fullmatch(pattern, text) for pattern in patterns), text)
        for text in ("5天一", "8天", "167:60", "选择", "", "1000:00"):
            self.assertFalse(any(re.fullmatch(pattern, text) for pattern in patterns), text)

    def test_label_fallback_keeps_exact_label_and_does_not_read_selection_line(self):
        original, fallback = self.module.label_conditions(88)
        self.assertEqual(original["roi"], [96, 610, 65, 30])
        self.assertEqual(fallback["roi"], [108, 611, 40, 16])
        self.assertTrue(fallback["only_rec"])
        self.assertEqual(original["expected"], fallback["expected"])
        for branch in (original, fallback):
            self.assertIsNone(re.fullmatch(branch["expected"][0], "偷窃选择"))

    def test_final_gate_requires_identity_no_preview_and_all_three_cooldowns(self):
        final = self.nodes["StealInteraction_AllCardsOnCooldown"]
        for name in ("NpcIdentity", "TalentHeading", "NoPreview", "Card1Cooldown", "Card2Cooldown", "Card3Cooldown"):
            self.assertIn("StealInteraction_" + name, final["all_of"])
        self.assertEqual(final["next"], [])
        self.assertEqual(final["on_error"], ["StealInteraction_Fail"])

    def test_preview_confirm_requires_exact_owner_rate_gold_and_button(self):
        for number in (1, 2, 3):
            confirm = self.nodes[f"StealInteraction_Card{number}Confirm"]
            self.assertEqual(confirm["all_of"], ["StealInteraction_Preview" + key
                                                 for key in ("Identity", "Rate", "Gold", "Button")])
            self.assertEqual(confirm["target"], [700, 454, 1, 1])
        patterns = self.module.interaction_overrides({"npc_names": ["老板.*"]})
        self.assertEqual(patterns["StealInteraction_NpcIdentity"]["expected"], [r"^老板\.\*$"])

    def run_action(self, results, params=None):
        context = SimpleNamespace(run_task=Mock(side_effect=results))
        context.clone = Mock(return_value=context)
        result = self.module.StealInteractionAction().run(context, SimpleNamespace(
            custom_action_param=params if params is not None else {"npc_names": ["目标老板"]}))
        return result, context

    def test_action_accepts_both_layouts_only_with_final_cooldown_gate(self):
        for offset in (0, 1):
            success, context = self.run_action([task([f"Offset{offset}"]), task(["AllCardsOnCooldown"])])
            self.assertTrue(success)
            calls = context.run_task.call_args_list
            self.assertEqual([call.args[0] for call in calls], ["StealInteraction_DetectEntry", "StealInteraction_Entry"])
            self.assertEqual(calls[1].args[1]["StealInteraction_Card1Select"]["target"][0], 126 if offset == 0 else 209)

    def test_missing_or_ambiguous_layout_never_enters_interaction(self):
        for result in (None, task([], False), task([]), task(["Offset0", "Offset1"])):
            success, context = self.run_action([result])
            self.assertFalse(success)
            self.assertEqual(context.run_task.call_count, 1)

    def test_native_success_without_final_gate_is_not_completion(self):
        for result in (None, task(["Card3Done"]), task(["AllCardsOnCooldown"], False)):
            success, _ = self.run_action([task(["Offset0"]), result])
            self.assertFalse(success)

    def test_invalid_identity_sends_no_subtask(self):
        for params in ({"npc_names": []}, {"npc_names": [" x"]}, {"npc_names": ["目标"], "force": True}):
            success, context = self.run_action([], params)
            self.assertFalse(success)
            context.run_task.assert_not_called()

    def test_failure_handler_never_converts_failure_to_success(self):
        flow = load_action("steal_flow")
        for value in ("bad json", {}, {"reason": "preview mismatch"}):
            self.assertFalse(flow.StealFailAction().run(None, SimpleNamespace(custom_action_param=value)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
