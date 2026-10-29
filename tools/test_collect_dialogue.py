"""Regression for character-pack dialogue blocking collection (2026-10-28)."""
import json
from pathlib import Path
import unittest

import cv2

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/resource/base"
PC = ROOT / "assets/resource/pc"
FIXTURES = ROOT / "tools/fixtures/collect_dialogue"
SKIP = "AutoBattle_Egress_TurnBack_SkipTalk"


def recognition_matches(pipeline, node, results, *, apply_inverse=False):
    """Evaluate composite routing with supplied visual recognition results."""
    if isinstance(node, str):
        if node in results:
            return results[node]
        node = pipeline[node]
    if node["recognition"] == "Custom":
        assert node["custom_recognition"] == "NotRecognition"
        return not recognition_matches(pipeline, node["custom_recognition_param"]["node"], results)
    if node["recognition"] == "Or":
        matched = any(recognition_matches(pipeline, child, results) for child in node["any_of"])
    else:
        assert node["recognition"] == "And"
        matched = all(recognition_matches(pipeline, child, results) for child in node["all_of"])
    # Native And/Or sub-recognitions ignore node-level inverse.
    return not matched if apply_inverse and node.get("inverse", False) else matched


class CollectDialogueTest(unittest.TestCase):
    def test_both_failed_runs_recognize_skip_button(self):
        pipeline = json.loads((BASE / "pipeline/Collect_Launcher.json").read_text())
        node = pipeline["Collect_FirstPack_Talk"]
        x, y, w, h = node["roi"]
        for fixture in sorted(FIXTURES.glob("*.png")):
            with self.subTest(fixture=fixture.name):
                image = cv2.imread(str(fixture))
                # Fixtures preserve the original recognition ROI only.
                self.assertEqual(image.shape[:2], (h, w))
                scores = []
                for template in node["template"]:
                    result = cv2.matchTemplate(
                        image, cv2.imread(str(BASE / "image" / template)),
                        cv2.TM_CCOEFF_NORMED,
                    )
                    scores.append(cv2.minMaxLoc(result)[1])
                self.assertGreaterEqual(max(scores), node.get("threshold", 0.7))
        self.assertEqual(len(list(FIXTURES.glob("*.png"))), 2)

    def test_sandbox_recovery_handles_dialogue(self):
        pipeline = json.loads((BASE / "pipeline/Global.json").read_text())
        for name in ("Global_ToSandBox", "Global_ToSandBox_Reset"):
            with self.subTest(node=name):
                candidates = pipeline[name]["next"]
                for competitor in ("Global_ToSandBox_Enter", "Global_ToSandBox_Reset"):
                    self.assertLess(candidates.index(f"[JumpBack]{SKIP}"), candidates.index(competitor))

    def test_dialogue_over_sandbox_does_not_bypass_recovery(self):
        pipeline = json.loads((BASE / "pipeline/Global.json").read_text())
        for marker in (False, True):
            for dialogue in (False, True):
                with self.subTest(marker=marker, dialogue=dialogue):
                    results = {"Rec_SandBox_Pin_Tpl": marker, "Rec_SandBox_Run_Tpl": marker, SKIP: dialogue}
                    ready = marker and not dialogue
                    self.assertEqual(recognition_matches(pipeline, "Global_ToSandBox_Enter", results), ready)
                    self.assertEqual(recognition_matches(pipeline, "Global_ToSandBox", results, apply_inverse=True), not ready)

    def test_pc_overlay_keeps_all_supported_skip_templates(self):
        base = json.loads((BASE / "pipeline/Collect_Launcher.json").read_text())
        pc = json.loads((PC / "pipeline/Collect_Launcher.json").read_text())
        # Pipeline overlays replace array fields; they do not append templates.
        node = {**base["Collect_FirstPack_Talk"], **pc["Collect_FirstPack_Talk"]}
        for template in ("UI_TalkSkip.png", "Chaga_ProductNext.png", "Chaga_ProductNext_PC.png"):
            with self.subTest(template=template):
                self.assertIn(template, node["template"])
                path = PC / "image" / template
                if not path.exists():
                    path = BASE / "image" / template
                image = cv2.imread(str(path))
                self.assertIsNotNone(image)
                self.assertLessEqual(image.shape[0], node["roi"][3])
                self.assertLessEqual(image.shape[1], node["roi"][2])
        self.assertTrue(node["green_mask"], "PC product-next template requires its green mask")


if __name__ == "__main__":
    unittest.main()
