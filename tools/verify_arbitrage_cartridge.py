"""卡带框归组、中线及编号完整裁剪回归；只用内存读数，不连接游戏或写存档。

运行：python -B tools/verify_arbitrage_cartridge.py -v
"""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from action import arbitrage_result as ar
from utils import arbitrage_store as store
from utils import arbitrage_cartridge as cart
from verify_arbitrage_number_geometry import row_image


def det(text, x, y, w, h):
    return dict(text=text, x=x, y=y, w=w, h=h, cx=x + w / 2, cy=y + h / 2, score=.99)


class Reader:
    def __init__(self, columns=None, number="14", image=None):
        self.columns = columns or {}
        self.number = number
        self.rois = []
        self.column_calls = []
        self.image = image if image is not None else np.full((720, 1280, 3), 40, np.uint8)
        if columns:
            groups, _ = ar._cart_groups(columns[ar._COL_CART])
            for group in groups:
                typed = group[0]
                value = ar._tail_num(ar._cart_group(group)[0]) or number
                # Ink lies inside padded OCR boxes, including the single-row upper boundary.
                row_image({**typed, "cy": typed["cy"] + 1}, value,
                          inline=bool(ar._tail_num(typed['text'])), image=self.image)
        self.tasker = NS(stopping=False, controller=NS(post_screencap=lambda: NS(wait=lambda: NS(get=lambda: self.image))))

    def get_node_object(self, node):
        return NS(recognition=NS(param=NS(roi=[960, 209, 102, 344])))

    def run_recognition(self, node, image, pipeline_override=None):
        if node == ar._RESCUE_NODE:
            self.rois.append(pipeline_override[node]["roi"])
            result = NS(text=self.number, score=.99, box=pipeline_override[node]["roi"])
            return NS(all_results=[result], filtered_results=[result])
        self.column_calls.append(node)
        return NS(filtered_results=[NS(text=d["text"], score=d["score"],
                                      box=[d[k] for k in ("x", "y", "w", "h")])
                                    for d in self.columns[node]])


class CartridgeGeometryTests(unittest.TestCase):
    def test_real_type_boxes_previously_rejected_at_75_and_79_percent(self):
        for name, current_y, base_y, ty, th, num_y, num_h, mon_ty, mon_h, mon_num_y in [
            ("卢戈山参烤串", 408, 439.5, 386, 18, 402, 16, 430, 14, 445),
            ("大蒜", 496, 527.5, 475, 17, 491, 14, 516, 17, 531),
        ]:
            with self.subTest(name=name):
                carts = [det("剧情游戏卡", 982, ty, 68, th), det("14", 1028, num_y, 21, num_h),
                         det("剧情游戏卡", 982, mon_ty, 68, mon_h), det("14", 1028, mon_num_y, 21, 17)]
                original = deepcopy(carts)
                day, month, bounds = ar._cart_regions(carts, current_y, base_y + 4, current_y + 88,
                                                      [960, 209, 102, 344], split_y=(current_y + base_y) / 2)
                reader = Reader(image=row_image(carts[0], "14"))
                result = ar._current_cart(day, reader, reader.image, ar._RESCUE_CFG, bounds, name)
                self.assertEqual(result[0], "剧情游戏卡14")
                self.assertEqual(ar._cart_group(month)[0], "剧情游戏卡14")
                self.assertEqual(carts, original)

    def test_number_follows_type_even_below_midline(self):
        carts = [det("剧情游戏卡", 980, 300, 70, 17), det("19", 1030, 321, 20, 17),
                 det("剧情游戏卡12", 960, 347, 90, 17)]
        day, month, _ = ar._cart_regions(carts, 310, 355, 400, [950, 200, 120, 350], split_y=327)
        self.assertEqual(ar._cart_group(day)[0], "剧情游戏卡19")
        self.assertEqual(ar._cart_group(month)[0], "剧情游戏卡12")
        self.assertEqual(day[0]["cy"], 308.5)

    def test_orphan_numbers_do_not_join_distant_or_complete_types(self):
        for typed, number in [
            (det("剧情游戏卡", 980, 300, 70, 17), det("19", 1030, 350, 20, 17)),
            (det("剧情游戏卡", 980, 300, 70, 17), det("19", 900, 321, 20, 17)),
            (det("剧情游戏卡5", 960, 300, 90, 17), det("19", 1030, 321, 20, 17)),
        ]:
            with self.subTest(typed=typed, number=number):
                groups, orphans = ar._cart_groups([typed, number])
                self.assertEqual(groups, [[typed]])
                self.assertEqual(orphans, [number])

    def test_duplicate_types_do_not_guess_number_owner(self):
        typed = det("剧情游戏卡", 980, 300, 70, 17)
        number = det("19", 1030, 321, 20, 17)
        groups, orphans = ar._cart_groups([typed, deepcopy(typed), number])
        self.assertEqual([len(g) for g in groups], [1, 1])
        self.assertEqual(orphans, [number])

    def test_same_line_separate_number_can_join_type(self):
        groups, orphans = ar._cart_groups([det("剧情游戏卡", 960, 300, 70, 17),
                                           det("5", 1032, 300, 15, 17)])
        self.assertEqual(ar._cart_group(groups[0])[0], "剧情游戏卡5")
        self.assertFalse(orphans)

    def test_name_and_base_midpoint_precedes_quote_fallback(self):
        names = [det("大蒜", 480, 482, 44, 28), det("45", 480, 514, 51, 27)]
        quotes = [det("54", 845, 478, 49, 24), det("54", 844, 518, 51, 27)]
        self.assertEqual(ar._cart_split_y(names, quotes, 496, 531, 584), 511.75)
        self.assertEqual(ar._cart_split_y(names[:1], quotes, 496, 531, 584), 510.75)

    def test_missing_or_ambiguous_quotes_cannot_guess_split(self):
        quote = det("54", 845, 478, 49, 24)
        self.assertIsNone(ar._cart_split_y([], [quote], 496, 531, 584))
        self.assertIsNone(ar._cart_split_y([], [quote, deepcopy(quote),
                                              det("54", 844, 518, 51, 27)], 496, 531, 584))

    def test_base_amount_can_locate_split_when_right_quotes_missing(self):
        self.assertEqual(ar._cart_split_y([det("45", 480, 514, 51, 27)], [], 496, None, 584), 511.75)

    def test_compatibility_rescue_uses_complete_pixel_crops(self):
        typed = det("剧情游戏卡", 982, 297, 67, 18)
        number = det("11", 1030, 314, 17, 14)
        reader = Reader(number="11", image=row_image(typed, "11"))
        result = ar._rescue_tail_num(reader, reader.image, [typed], ar._RESCUE_CFG,
                                     (960, 280, 1062, 346), [number])
        self.assertEqual(result[0], "11")
        self.assertGreaterEqual(len(reader.rois), 2)
        self.assertEqual(len(reader.rois), len({tuple(r) for r in reader.rois}))
        self.assertEqual({r[1] for r in reader.rois}, {311, 313})
        self.assertEqual(len(reader.rois), 4)
        for x, y, w, h in reader.rois:
            self.assertLessEqual(y + h, 346)

    def test_reference_number_cannot_expand_crop_into_monthly_row(self):
        typed = det("剧情游戏卡", 980, 300, 70, 17)
        reader = Reader(image=row_image(typed, "14"))
        number = det("19", 1030, 337, 20, 24)
        ar._rescue_tail_num(reader, reader.image, [typed],
                           ar._RESCUE_CFG, (960, 280, 1062, 333), [number])
        self.assertTrue(reader.rois)
        self.assertTrue(all(y + h <= 333 for x, y, w, h in reader.rois))
        self.assertGreater(len({r[3] for r in reader.rois}), 1)

    def test_parser_reuses_four_columns_and_keeps_day_month_separate(self):
        columns = {
            ar._COL_NAME: [det("大蒜", 480, 300, 44, 24), det("45", 480, 332, 51, 24)],
            ar._COL_AMOUNT: [det("54", 845, 294, 49, 24), det("54", 844, 336, 51, 24)],
            ar._COL_PRICE: [det("120%", 898, 294, 60, 24), det("120%", 898, 336, 60, 24)],
            ar._COL_CART: [det("剧情游戏卡", 982, 290, 68, 17), det("14", 1030, 306, 18, 14),
                           det("角色游戏卡4", 966, 332, 83, 17)],
        }
        for with_base in (True, False):
            with self.subTest(with_base=with_base):
                source = deepcopy(columns)
                if not with_base:
                    source[ar._COL_NAME] = source[ar._COL_NAME][:1]
                reader = Reader(source)
                rows = ar.ArbitrageSellController()._parse_current_page(reader)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["target_cartridge"], "剧情游戏卡14")
                self.assertEqual(rows[0]["monthly_cartridge"], "角色游戏卡4")
                self.assertTrue(rows[0]["is_max_price"])
                self.assertEqual(reader.column_calls, list(columns))

    def test_previous_parser_snapshot_requires_rescan(self):
        data = {"arbitrage": {"market": {"days": {"today": {
            "complete": True, "parser_version": 2, "items": []}}}}}
        with patch.object(store.SharedStore, "load", return_value=data):
            self.assertIsNone(store.get_market_snapshot("today"))


class CartridgeRescueTests(unittest.TestCase):
    def choose(self, type_text, number, *, score=.99, reads="19", inline=False, cfg=None):
        typed = det(type_text + (number if inline else ""), 914, 307, 68, 14)
        rows = [typed] if inline else [typed, {**det(number, 965, 320, 15, 14), "score": score}]
        reader = Reader(number=reads, image=row_image(typed, number if len(number) == 2 else reads, inline=inline))
        result = cart.read_current(rows, reader, reader.image, cfg or cart.DEFAULT_CONFIG,
                                   (908, 302, 990, 346), "测试")
        return result, reader

    def test_only_inline_complete_numbers_can_skip_reread(self):
        for inline, number in [(True, "9"), (False, "19"), (False, "11")]:
            result, reader = self.choose("故事遊戲卡带", number, inline=inline, reads=number)
            self.assertEqual(result[0], "剧情游戏卡" + number)
            self.assertEqual(len(reader.rois), 0 if inline else 4)

    def test_ambiguous_single_digit_rereads_leading_one(self):
        result, reader = self.choose("故事遊戲卡带", "9")
        self.assertEqual(result[0], "剧情游戏卡19")
        self.assertTrue(reader.rois)

    def test_event_single_digit_also_uses_geometry(self):
        for number in ("5", "6", "7"):
            with self.subTest(number=number):
                result, reader = self.choose("活動遊戲卡带", number, reads=number)
                self.assertEqual(result[0], "活动游戏卡" + number)
                self.assertEqual(len(reader.rois), 4)

    def test_known_type_prefix_survives_shared_suffix_errors(self):
        result, reader = self.choose("故事避戲卡带", "19")
        self.assertEqual(result[0], "剧情游戏卡19")
        self.assertEqual(len(reader.rois), 4)

    def test_four_type_funnel_does_not_guess_from_shared_suffix(self):
        self.assertEqual(cart.classify_type("故遊戲卡帶"), ("story", False))
        self.assertEqual(cart.classify_type("遊戲卡帶"), ("", False))
        self.assertEqual(cart.classify_type("角色故事遊戲卡"), ("", False))
        self.assertEqual(cart.classify_type("店長遊戲卡帶"), ("manager", True))

    def test_family_number_ranges_are_used(self):
        self.assertTrue(cart.valid_number("19", "story", cart.DEFAULT_CONFIG))
        for number, family in [("19", "character"), ("8", "event"), ("15", "event"), ("1", "manager"), ("0", "story")]:
            self.assertFalse(cart.valid_number(number, family, cart.DEFAULT_CONFIG))

    def test_general_bad_digit_can_be_corrected_not_only_prefixed(self):
        result, _ = self.choose("故事遊戲卡带", "0", score=.4, reads="9")
        self.assertEqual(result[0], "剧情游戏卡9")

    def test_low_score_two_digits_are_not_shortened_by_crops(self):
        result, _ = self.choose("故事遊戲卡带", "11", score=.4, reads="1")
        self.assertEqual(result[0], "剧情游戏卡")
        self.assertEqual(result[2], "unconfirmed_current_number")

    def test_nearby_high_scores_do_not_resolve_number_conflict(self):
        evidence = {"9": [{"score": .99, "roi": [1, 1, 20, 12]}],
                    "19": [{"score": .95, "roi": [1, 4, 20, 12]}]}
        self.assertEqual(cart._choose_number(evidence, .85, 2)[2], "complete_crop_conflict")
        evidence["9"][0]["score"] = .64
        self.assertEqual(cart._choose_number(evidence, .85, 2)[2], "complete_crop_conflict")

    def test_width_variants_at_same_top_do_not_stack_votes(self):
        evidence = {"9": [{"score": .99, "roi": [1, 1, 20, 12]},
                           {"score": .99, "roi": [1, 1, 30, 12]}]}
        self.assertEqual(cart._choose_number(evidence, .85, 1)[2], "insufficient_positions")


    def test_config_is_per_run_and_defaults_are_not_mutated(self):
        default = deepcopy(cart.DEFAULT_CONFIG)
        context = NS(get_node_object=lambda _: NS(attach={"type_height": 12,
                         "type_labels": {"story": "故事遊戲卡帶"}}))
        first = cart.load_config(context)
        first["number_ranges"]["story"][1] = 99
        context.get_node_object = lambda _: NS(attach={})
        self.assertEqual(cart.load_config(context), default)
        self.assertEqual(cart.DEFAULT_CONFIG, default)

    def test_large_box_is_redetected_before_default_height_scan(self):
        calls = []
        def recognize(node, image, pipeline_override):
            calls.append(pipeline_override[node])
            if pipeline_override[node]["only_rec"]:
                return NS(all_results=[NS(text="5", score=.99, box=pipeline_override[node]["roi"])])
            return NS(filtered_results=[NS(text="活動遊戲卡帶", score=.95, box=[914, 308, 66, 14]),
                                        NS(text="5", score=.99, box=[972, 321, 7, 12])])
        context = NS(run_recognition=recognize)
        image = row_image(det("活動遊戲卡帶", 914, 308, 66, 14), "5")
        result = cart.read_current([det("活動遊戲卡带", 908, 302, 79, 41)], context, image,
                                   cart.DEFAULT_CONFIG, (908, 300, 990, 346), "测试")
        self.assertEqual(result[0], "活动游戏卡5")
        self.assertEqual(len(calls), 5)
        self.assertFalse(calls[0]["only_rec"])

    def test_missing_detection_can_use_independent_current_region(self):
        calls = []
        def recognize(node, image, pipeline_override):
            roi = pipeline_override[node]["roi"]
            calls.append(roi)
            if roi[2] > 60:
                text = "故事遊戲卡帶" if roi[1] <= 313 and roi[1] + roi[3] >= 322 else ""
            else:
                text = "19"
            return NS(filtered_results=[NS(text=text, score=.99, box=roi)])
        image = row_image(det("故事遊戲卡帶", 914, 310, 66, 14), "19")
        result = cart.read_current([], NS(run_recognition=recognize), image, cart.DEFAULT_CONFIG,
                                   (908, 300, 990, 346), "测试")
        self.assertEqual(result[0], "剧情游戏卡19")
        self.assertEqual(result[3]["geometry_basis"], "pixel_bands")
        self.assertLessEqual(len(calls), 2 * cart.DEFAULT_CONFIG["max_scan_windows"])

    def test_repeated_last_page_can_fill_a_previously_missing_number(self):
        item = {"name": "马铃薯", "target_cartridge": "剧情游戏卡", "current_rate": 100,
                "current_price": 5, "is_max_price": False}
        fresh = {**item, "target_cartridge": "剧情游戏卡2", "current_cartridge": "剧情游戏卡2"}
        controller = ar.ArbitrageSellController()
        ctx = NS(tasker=NS(stopping=False), run_task=lambda _: NS(status=NS(succeeded=True)))
        with patch.object(controller, "_parse_current_page", side_effect=[[item], [fresh]]):
            scan = controller._scan_price_list(ctx, 3, False)
        self.assertTrue(scan["complete"])
        self.assertTrue(scan["cartridges_complete"])
        self.assertEqual(scan["items"][0]["target_cartridge"], "剧情游戏卡2")

    def test_worse_repeat_does_not_erase_good_number_and_conflict_is_retained(self):
        saved = {"target_cartridge": "剧情游戏卡2", "current_price": 5}
        ar._merge_cartridge_observation(saved, {"target_cartridge": "剧情游戏卡", "current_price": 5})
        self.assertEqual(saved["target_cartridge"], "剧情游戏卡2")
        ar._merge_cartridge_observation(saved, {"target_cartridge": "剧情游戏卡3", "current_price": 5})
        self.assertEqual(saved["target_cartridge"], "")
        self.assertTrue(saved["cart_conflict"])

    def test_complete_crop_conflict_overrides_previous_good_page(self):
        saved = {"target_cartridge": "剧情游戏卡5", "current_price": 29}
        conflict = {"target_cartridge": "剧情游戏卡", "cart_conflict": True,
                    "cartridge_evidence": {"decision_reason": "complete_crop_conflict",
                                           "number_candidates": ["3", "5"]}}
        ar._merge_cartridge_observation(saved, conflict)
        self.assertTrue(saved["cart_conflict"])
        self.assertEqual(saved["target_cartridge"], "")
        self.assertEqual(saved["cartridge_evidence"], conflict["cartridge_evidence"])
        ar._merge_cartridge_observation(saved, {"target_cartridge": "剧情游戏卡5"})
        self.assertEqual(saved["target_cartridge"], "")

    def test_version_five_cache_does_not_survive_failed_version_six_scan(self):
        original = {"untouched": {"gold_fact": 123}, "arbitrage": {"market": {"days": {"today": {
            "complete": True, "parser_version": 5, "items": [{"name": "咖啡豆", "target_cartridge": "剧情游戏卡3"}]}}}}}
        data = deepcopy(original)
        self.assertEqual(store.MARKET_PARSER_VERSION, 6)
        with patch.object(store.SharedStore, "load", return_value=data), \
             patch.object(store.SharedStore, "save", return_value=True) as save:
            self.assertIsNone(store.get_market_snapshot("today"))
            store.save_market_snapshot({"complete": False, "items": []}, "today")
            self.assertIsNone(store.get_market_snapshot("today"))
            self.assertEqual(data["arbitrage"]["market"]["days"]["today"]["parser_version"], 6)
            self.assertEqual(data["untouched"], original["untouched"])
            save.assert_called_once()

    def test_serialized_number_evidence_keeps_low_score_rival_and_no_image(self):
        import json
        evidence = {"geometry_version": 1, "number_candidates": ["3", "5"],
                    "crops": [{"roi": [965, 320, 18, 14], "admitted": True,
                               "reads": [{"raw_text": "3", "number": "3", "score": .2, "supporting": False}]}]}
        items = store._normalized_market_items([{"name": "咖啡豆", "cart_conflict": True,
                                                 "cartridge_evidence": evidence}])
        self.assertEqual(json.loads(json.dumps(items))[0]["cartridge_evidence"], evidence)


if __name__ == "__main__":
    unittest.main()
