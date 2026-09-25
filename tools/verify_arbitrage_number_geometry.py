"""Synthetic pixel and decision tests; no game controller or user storage writes."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import arbitrage_cartridge as cart
from utils import arbitrage_number_geometry as geo


def det(text, x=914, y=307, w=66, h=14, score=.99):
    return dict(text=text, x=x, y=y, w=w, h=h, cx=x + w / 2, cy=y + h / 2, score=score)


def row_image(typed, number="5", *, inline=False, image=None):
    """Draw separated glyph blocks, not a screenshot or a mock geometry result."""
    if image is None:
        image = np.full((720, 1280, 3), 40, dtype=np.uint8)
    top = int(typed["cy"]) - 4
    image[top:top + 9, typed["x"]:typed["x"] + typed["w"]] = 200
    if not inline:
        number_top = top + 11
        right = typed["x"] + typed["w"]
        for index in range(len(number)):
            x = right - 5 - (len(number) - index - 1) * 7
            image[number_top:number_top + 10, x:x + 5] = 220
    return image


class NumberReader:
    def __init__(self, number="5", *, readings=None, image=None):
        self.number = number
        self.readings = readings
        self.calls = []
        self.image = image
        self.tasker = NS(stopping=False)

    def run_recognition(self, node, image, pipeline_override=None):
        param = pipeline_override[node]
        self.calls.append(param)
        value = self.readings(len(self.calls), param) if self.readings else [(self.number, .99)]
        results = [NS(text=text, score=score, box=param["roi"]) for text, score in value]
        return NS(all_results=results, filtered_results=[r for r in results if r.score >= .3])


class NumberGeometryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = deepcopy(cart.DEFAULT_CONFIG)
        self.cfg.update(number_height=12, number_right_pad=9)
        self.typed = det("故事遊戲卡帶")
        self.image = row_image(self.typed)
        self.bounds = [908, 302, 990, 346]

    def region(self, image=None, bounds=None):
        return geo.analyze_number_region(self.image if image is None else image,
                                         self.bounds if bounds is None else bounds, [self.typed], self.cfg)

    def test_whole_digit_boxes_have_two_safe_tops_and_no_private_pixels_in_detail(self):
        region = self.region()
        specs = geo.build_number_crops(region, self.cfg)
        self.assertEqual(region.detail["status"], "wrapped_bounded")
        self.assertEqual(len(specs), 4)
        self.assertEqual(len({tuple(s["roi"]) for s in specs}), 4)
        self.assertEqual(len({s["roi"][1] for s in specs}), 2)
        self.assertTrue(all(s["admitted"] for s in specs))
        json.dumps(region.detail)

    def test_clipped_glyph_is_rejected_on_every_edge(self):
        region = self.region()
        x, y, w, h = region.detail["number_box"]
        for roi in ([x + 1, y, w - 1, h], [x, y, w - 1, h],
                    [x, y + 1, w, h - 1], [x, y, w, h - 1]):
            with self.subTest(roi=roi):
                self.assertEqual(geo.admit_number_crop(region, roi)["reason"], "cut_number")

    def test_empty_type_overlap_and_outside_row_are_rejected(self):
        region = self.region()
        for roi, reason in [([960, 338, 24, 5], "cut_number"),
                            ([960, 310, 24, 30], "includes_type"),
                            ([907, 319, 80, 20], "out_of_bounds")]:
            with self.subTest(roi=roi):
                self.assertEqual(geo.admit_number_crop(region, roi)["reason"], reason)

    def test_invalid_images_and_bounds_cannot_qualify(self):
        for image in (None, object(), np.zeros((1080, 1920, 3), np.uint8),
                      np.zeros((720, 1280), np.uint8), self.image.astype(float)):
            self.assertEqual(geo.analyze_number_region(image, self.bounds, [self.typed], self.cfg).detail["status"], "geometry_unknown")
        for bounds in ([908, 302, 900, 346], [-1, 302, 990, 346], [908, 302, 990, 721],
                       [908, float('nan'), 990, 346], [True, 302, 990, 346], [908]):
            self.assertEqual(self.region(bounds=bounds).detail["status"], "geometry_unknown")

    def test_uniform_pixels_are_not_text(self):
        self.assertEqual(self.region(image=np.full_like(self.image, 50)).detail["reason"], "uniform_or_empty")

    def test_native_short_side_rounding_matches_startup_aspect_policy(self):
        from startup.pc import ASPECT_TOLERANCE
        self.assertEqual(geo.ASPECT_TOLERANCE, ASPECT_TOLERANCE)
        self.assertEqual(self.region(image=self.image[:, :1277]).detail["status"], "wrapped_bounded")
        self.assertEqual(self.region(image=self.image[:, :1200]).detail["reason"], "invalid_image")

    def test_current_bounds_cannot_cut_or_include_monthly_line(self):
        self.assertEqual(self.region(bounds=[908, 325, 990, 346]).detail["status"], "geometry_unknown")
        image = self.image.copy()
        image[340:344, 914:980] = 200
        self.assertEqual(self.region(image=image).detail["reason"], "ambiguous_bands")

    def test_type_box_may_miss_a_pixel_but_center_must_still_anchor(self):
        typed = {**self.typed, "y": 311, "h": 6, "cy": 314}
        result = geo.analyze_number_region(self.image, self.bounds, [typed], self.cfg)
        self.assertEqual(result.detail["status"], "wrapped_bounded")
        typed["cy"] = 337
        self.assertEqual(geo.analyze_number_region(self.image, self.bounds, [typed], self.cfg).detail["reason"], "type_band_unanchored")

    def test_digit_blocks_are_structural_not_an_ocr_number(self):
        for number in ("1", "5", "10", "11", "15", "19"):
            region = self.region(image=row_image(self.typed, number))
            self.assertEqual(len(region.detail["blocks"]), len(number))
            self.assertNotIn("number", region.detail)


class NumberReadingTests(unittest.TestCase):
    def read(self, *, typed=None, original="5", actual="5", readings=None, inline=False, context=None):
        typed = typed or det("故事遊戲卡帶" + (original if inline else ""))
        image = row_image(typed, actual, inline=inline)
        detections = [typed] if inline else [typed, det(original, 974, 322, 6, 12)]
        reader = context or NumberReader(actual, readings=readings)
        result = cart.read_current(detections, reader, image, cart.DEFAULT_CONFIG, [908, 302, 990, 346], "测试")
        return result, reader

    def test_single_and_two_digit_wrapped_values_are_all_verified(self):
        for number in ("1", "5", "10", "11", "15", "19"):
            result, reader = self.read(original=number, actual=number)
            self.assertEqual(result[0], "剧情游戏卡" + number)
            self.assertEqual(len(reader.calls), 4)
            self.assertEqual(result[3]["decision_reason"], "complete_crop_consensus")
            json.dumps({k: v for k, v in result[3].items() if k != "attempts"})

    def test_inline_complete_value_does_not_use_wrapped_number_crops(self):
        result, reader = self.read(inline=True, original="9")
        self.assertEqual(result[0], "剧情游戏卡9")
        self.assertEqual(reader.calls, [])

    def test_complete_crops_correct_initial_missed_one_and_six_nine_error(self):
        for original, actual in (("5", "15"), ("6", "9"), ("0", "10")):
            result, _ = self.read(original=original, actual=actual)
            self.assertEqual(result[0], "剧情游戏卡" + actual)

    def test_low_score_legal_rival_is_not_lost_to_filtered_results(self):
        result, reader = self.read(readings=lambda i, p: [("3", .2)] if i == 4 else [("5", .99)])
        self.assertEqual(result[0], "剧情游戏卡")
        self.assertEqual(result[3]["number_candidates"], ["3", "5"])
        self.assertEqual(result[3]["decision_reason"], "complete_crop_conflict")
        self.assertEqual(len(reader.calls), 4)

    def test_same_number_with_only_one_high_score_position_stays_unknown(self):
        result, _ = self.read(readings=lambda i, p: [("5", .99 if i <= 2 else .7)])
        self.assertEqual(result[3]["decision_reason"], "insufficient_positions")

    def test_two_digit_shape_does_not_accept_shortened_consensus(self):
        result, _ = self.read(actual="15", readings=lambda i, p: [("5", .99)])
        self.assertEqual(result[3]["decision_reason"], "digit_shape_disagreement")

    def test_range_and_no_character_repair(self):
        for raw in ("0", "05", "71", "M5", "5?", "_"):
            result, _ = self.read(readings=lambda i, p: [(raw, .99)])
            self.assertEqual(result[3]["decision_reason"], "no_legal_number")
        result, _ = self.read(typed=det("活動遊戲卡帶"), actual="15", original="15")
        self.assertEqual(result[0], "活动游戏卡")
        self.assertEqual(result[3]["decision_reason"], "no_legal_number")

    def test_exception_and_stop_do_not_confirm_partial_consensus(self):
        def fail(i, p):
            if i == 4:
                raise RuntimeError("injected OCR failure")
            return [("5", .99)]
        result, _ = self.read(readings=fail)
        self.assertEqual(result[3]["decision_reason"], "ocr_error")
        reader = NumberReader()
        def stop(i, p):
            reader.tasker.stopping = True
            return [("5", .99)]
        reader.readings = stop
        result, _ = self.read(context=reader)
        self.assertEqual(result[3]["decision_reason"], "stopped")
        self.assertNotIn("number", result[3])

    def test_config_limits_and_defaults_do_not_mutate(self):
        original = deepcopy(cart.DEFAULT_CONFIG)
        for key, values in {
            "number_crop_x_padding_fracs": ([True, 1], [.5], [1, .5], [-1, 1], [.5, float('nan')]),
            "number_band_height_ratio": ([0, 1], [2, 1], [.5, 5]),
            "number_crop_bottom_padding": (-1, True, 1.5),
        }.items():
            for value in values:
                context = NS(get_node_object=lambda _: NS(attach={key: value}))
                self.assertEqual(cart.load_config(context)[key], original[key])
        good = cart.load_config(NS(get_node_object=lambda _: NS(attach={"number_crop_bottom_padding": 0})))
        self.assertEqual(good["number_crop_bottom_padding"], 0)
        self.assertEqual(cart.DEFAULT_CONFIG, original)


if __name__ == "__main__":
    unittest.main()
