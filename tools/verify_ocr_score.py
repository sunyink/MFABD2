"""OCR confidence selection and native Or routing; no game or real saves.

Run: python -B tools/verify_ocr_score.py -v
Native routing uses captured OCR candidates and a fallback recognition fixture;
it verifies callback boxes and priority, not live OCR accuracy or device input.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.define import LoggingLevelEnum, OCRResult, Rect
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer

with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "_set_api_properties"), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls):
    from action.arbitrage_result import _sell_item_override
    from action.arbitrage_buy_precise import buy_overrides
    from recognition.ocr_score import OCRBestScore
from utils.ocr_score import select_best_ocr
from verify_arbitrage_purchase_cycle import OfflineController


PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
SOURCE = "Agt_<Sell_Item>_Ocr"
SELECTOR = "Agt_<Sell_Item>_OcrScore"
PARENT = "Arbitrage_Sell_Item_ListTraverse"
TEMPLATE = "Agt_<Sell_Item>_Tmp"
# Exact candidates from the failed rice lookup, reco_id 400010275.
RICE = [
    {"box": [595, 267, 9, 8], "score": 0.528964, "text": "米"},
    {"box": [839, 446, 19, 18], "score": 0.999924, "text": "米"},
]


def result(candidates, *, hit=True):
    return NS(hit=hit, reco_id=400010275, filtered_results=candidates, all_results=RICE)


class OcrScoreTests(unittest.TestCase):
    def test_rice_candidates_choose_real_name_without_mutation(self):
        candidates = deepcopy(RICE)
        self.assertEqual(select_best_ocr(candidates), RICE[1])
        self.assertEqual(candidates, RICE)
        objects = [OCRResult(box=Rect(*r["box"]), score=r["score"], text=r["text"]) for r in RICE]
        self.assertEqual(select_best_ocr(objects), RICE[1])

    def test_ties_keep_source_order_and_low_scores_are_not_rethresholded(self):
        same_score = [{**RICE[0], "score": 0.5}, {**RICE[1], "score": 0.5}]
        self.assertEqual(select_best_ocr(same_score), same_score[0])
        self.assertEqual(select_best_ocr(RICE[:1]), RICE[0])
        self.assertIsNone(select_best_ocr([]))

    def test_malformed_candidates_cannot_supply_click_boxes(self):
        invalid = [{**RICE[1], "score": v} for v in (float("nan"), float("inf"), None, True)]
        invalid += [{**RICE[1], "box": v} for v in (None, [1, 2], [1, 2, 0, 4], [-1, 2, 3, 4])]
        self.assertIsNone(select_best_ocr(invalid))
        self.assertEqual(select_best_ocr(invalid + RICE), RICE[1])

    def test_adapter_runs_source_once_on_the_supplied_frame(self):
        image = object()
        context = NS(get_node_data=Mock(return_value={"recognition": {"type": "OCR"}}),
                     run_recognition=Mock(return_value=result(RICE)))
        args = NS(image=image, custom_recognition_param=json.dumps({"node": SOURCE}))
        selected = OCRBestScore().analyze(context, args)
        self.assertEqual(selected.box, RICE[1]["box"])
        self.assertEqual(selected.detail["source_reco_id"], 400010275)
        self.assertEqual(selected.detail["candidate_count"], 2)
        context.run_recognition.assert_called_once_with(SOURCE, image)

    def test_adapter_never_promotes_rejected_all_results(self):
        context = NS(get_node_data=lambda _: {"recognition": {"type": "OCR"}})
        args = NS(image=object(), custom_recognition_param={"node": SOURCE})
        for source_result in (result([]), result(RICE, hit=False), None):
            with self.subTest(result=source_result):
                context.run_recognition = Mock(return_value=source_result)
                self.assertIsNone(OCRBestScore().analyze(context, args).box)

    def test_adapter_rejects_non_ocr_sources_before_nested_recognition(self):
        context = NS(get_node_data=lambda _: {"recognition": {"type": "Custom"}},
                     run_recognition=Mock())
        args = NS(image=object(), custom_recognition_param={"node": SELECTOR})
        with patch("recognition.ocr_score.mfaalog.error"):
            self.assertIsNone(OCRBestScore().analyze(context, args).box)
        context.run_recognition.assert_not_called()

    def test_sale_and_precise_buy_keep_ocr_first_and_cartridge_matching(self):
        context = NS(get_node_object=lambda n: NS(attach=PIPE[n].get("attach", {})))
        sale = _sell_item_override(context, "米")
        self.assertEqual(sale[PARENT]["any_of"], [SELECTOR, TEMPLATE])
        self.assertEqual(sale[SOURCE]["expected"], [])
        self.assertEqual(sale[SELECTOR]["custom_recognition"], "OCRItemName")
        self.assertEqual(sale[SELECTOR]["custom_recognition_param"]["item_name"], "米")
        self.assertEqual(_sell_item_override(context, "未知商品")[PARENT]["any_of"], [SELECTOR])
        request = {"item_name": "米", "cartridge": "剧情游戏卡12"}
        buying = buy_overrides(context, request)
        self.assertEqual(buying[PARENT]["any_of"], [SELECTOR, TEMPLATE])
        self.assertEqual(buying[SOURCE], sale[SOURCE])
        self.assertEqual(set(buying["Arbitrage_Sell_PackShopSwich"]), {"expected"})
        self.assertEqual(buying["Arbitrage_Sell_Item_Click"]["target_offset"], [0, 30, 0, 0])


class Fallback(CustomRecognition):
    calls = 0

    def analyze(self, context, argv):
        self.calls += 1
        return self.AnalyzeResult(box=[10, 20, 30, 40], detail={})


class CaptureBox(CustomAction):
    def __init__(self):
        super().__init__()
        self.boxes = []

    def run(self, context, argv):
        self.boxes.append(list(argv.box))
        return True


class NativeRoutingTests(unittest.TestCase):
    def test_base_and_pc_route_selected_box_or_template_fallback(self):
        with tempfile.TemporaryDirectory(prefix="mfabd2-ocr-score-", ignore_cleanup_errors=True) as logdir:
            Tasker.set_log_dir(logdir)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            try:
                for pc in (False, True):
                    with self.subTest(pc=pc):
                        self.run_case(pc)
            finally:
                Tasker.set_log_dir("")

    def run_case(self, pc):
        if pc and not (ROOT / "assets/resource/pc/pipeline/Arbitrage.json").is_file():
            self.skipTest("PC arbitrage overlay is not present on this branch")
        resource = Resource()
        self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
        if pc:
            self.assertTrue(resource.post_bundle(ROOT / "assets/resource/pc").wait().succeeded)
        self.assertEqual(resource.get_node_data(PARENT)["recognition"]["param"]["any_of"],
                         [SELECTOR, TEMPLATE])
        fallback, capture = Fallback(), CaptureBox()
        self.assertTrue(resource.register_custom_recognition("OCRBestScore", OCRBestScore()))
        self.assertTrue(resource.register_custom_recognition("test_fallback", fallback))
        self.assertTrue(resource.register_custom_action("test_capture", capture))
        self.assertTrue(resource.override_pipeline({
            PARENT: {"action": "Custom", "custom_action": "test_capture", "next": [],
                     "pre_delay": 0, "post_delay": 0, "timeout": 1, "rate_limit": 0},
            TEMPLATE: {"recognition": "Custom", "custom_recognition": "test_fallback"},
        }))
        controller = OfflineController()
        self.assertTrue(controller.post_connection().wait().succeeded)
        tasker = Tasker()
        self.assertTrue(tasker.bind(resource, controller))
        try:
            for candidates in (RICE, []):
                observed = []

                def source_ocr(context, name, image, *args, **kwargs):
                    observed.append((name, image.shape))
                    return result(candidates)

                with patch.object(Context, "run_recognition", source_ocr):
                    detail = tasker.post_task(PARENT).wait().get()
                self.assertTrue(detail.status.succeeded)
                self.assertEqual(observed, [(SOURCE, (720, 1280, 3))])
                self.assertEqual(fallback.calls, 0 if candidates else 1)
                self.assertEqual(capture.boxes[-1], RICE[1]["box"] if candidates else [10, 20, 30, 40])
        finally:
            tasker.post_stop().wait()
            del tasker
            del controller
            resource.clear()


if __name__ == "__main__":
    unittest.main()
