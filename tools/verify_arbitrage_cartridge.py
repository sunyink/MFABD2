"""卡带框归组、中线及编号完整裁剪回归；只用内存读数，不连接游戏或写存档。

运行：python -B tools/verify_arbitrage_cartridge.py -v
"""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from action import arbitrage_result as ar
from utils import arbitrage_store as store


def det(text, x, y, w, h):
    return dict(text=text, x=x, y=y, w=w, h=h, cx=x + w / 2, cy=y + h / 2, score=.99)


class Reader:
    def __init__(self, columns=None, number="14"):
        self.columns = columns or {}
        self.number = number
        self.rois = []
        self.column_calls = []
        self.tasker = NS(controller=NS(post_screencap=lambda: NS(wait=lambda: NS(get=lambda: object()))))

    def get_node_object(self, node):
        return NS(recognition=NS(param=NS(roi=[960, 209, 102, 344])))

    def run_recognition(self, node, image, pipeline_override=None):
        if node == ar._RESCUE_NODE:
            self.rois.append(pipeline_override[node]["roi"])
            return NS(filtered_results=[NS(text=self.number, score=.99)])
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
                reader = Reader()
                result = ar._current_cart(day, reader, object(), ar._RESCUE_CFG, bounds, name)
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

    def test_rescue_crops_preserve_whole_detected_number_and_day_boundary(self):
        typed = det("剧情游戏卡", 982, 297, 67, 18)
        number = det("11", 1030, 314, 17, 14)
        reader = Reader(number="11")
        result = ar._rescue_tail_num(reader, object(), [typed], ar._RESCUE_CFG,
                                     (960, 280, 1062, 346), [number])
        self.assertEqual(result[0], "11")
        self.assertGreaterEqual(len(reader.rois), 2)
        self.assertEqual(len(reader.rois), len({tuple(r) for r in reader.rois}))
        for x, y, w, h in reader.rois:
            self.assertLessEqual(x, 1030)
            self.assertLessEqual(y, 314)
            self.assertGreaterEqual(x + w, 1047)
            self.assertGreaterEqual(y + h, 328)
            self.assertLessEqual(y + h, 346)

    def test_number_crossing_monthly_type_cannot_be_cropped_as_current(self):
        reader = Reader()
        number = det("19", 1030, 337, 20, 24)
        self.assertEqual(ar._rescue_tail_num(reader, object(), [det("剧情游戏卡", 980, 300, 70, 17)],
                                            ar._RESCUE_CFG, (960, 280, 1062, 347), [number]), ("", 0.0))
        self.assertFalse(reader.rois)

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


if __name__ == "__main__":
    unittest.main()
