"""Unknown-name and price rescue regressions; all OCR and storage are in memory."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from utils import ocr_item_name as names
from utils import arbitrage_pricelist as prices
from utils import arbitrage_cartridge as cart
from action import arbitrage_result as ar


class NameTests(unittest.TestCase):
    def test_real_one_character_variants_share_catalog_without_manual_aliases(self):
        for raw, expected in (("甜辣鲜蝦", "甜辣鲜虾"), ("包装好的海苔", "调味海苔"),
                              ("蘿萄嬰", "萝卜缨"), ("盧戈山蓼", "卢戈山参"),
                              ("強化夹板", "强化胶合板"), ("銅鐵塊", "钢铁块")):
            with self.subTest(raw=raw):
                result = names.resolve_ocr_name(raw, .9)
                self.assertEqual(result["name"], expected)
                self.assertTrue(result["confirmed"])
                self.assertEqual(result["raw"], raw)

    def test_aliases_of_same_item_do_not_make_a_false_conflict(self):
        result = names.resolve_ocr_name("甜辣鲜蝦", aliases={"甜辣鲜虾": "虾菜", "甜辣鮮蝦": "虾菜"})
        self.assertEqual(result["name"], "虾菜")

    def test_valid_short_item_is_not_fuzzy_corrected(self):
        self.assertEqual(names.resolve_ocr_name("铜块")["name"], "铜块")

    def test_unknown_short_item_requires_a_reread(self):
        result = names.resolve_ocr_name("銅快")
        self.assertFalse(result["confirmed"])
        result = names.resolve_ocr_name("銅快", reread=lambda: [("銅塊", .99)])
        self.assertEqual(result["name"], "铜块")

    def test_short_unique_spelling_requires_two_consistent_rereads(self):
        result = names.resolve_ocr_name("夹板", reread=lambda: [("夹板", .99)])
        self.assertFalse(result["confirmed"])
        result = names.resolve_ocr_name("夹板", reread=lambda: [("夹板", .99), ("夹板", .98)])
        self.assertEqual(result["name"], "胶合板")

    def test_competing_catalog_items_stay_unknown_until_exact_reread(self):
        aliases = {"香甜面包": "A", "香甜蛋糕": "B"}
        result = names.resolve_ocr_name("香甜面糕", aliases=aliases)
        self.assertFalse(result["confirmed"])
        result = names.resolve_ocr_name("香甜面糕", aliases=aliases,
                                        reread=lambda: [("香甜蛋糕", .9)])
        self.assertEqual(result["name"], "B")

    def test_invented_item_is_not_forced_into_catalog(self):
        result = names.resolve_ocr_name("完全未知的新商品")
        self.assertFalse(result["confirmed"])


def det(text, y, score=.99):
    return dict(text=text, x=80, y=y-5, w=15, h=10, cx=87.5, cy=y, score=score)


class PriceTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((100, 120, 3), dtype=np.uint8)
        self.frame[15:25, 91:99] = 245
        self.cfg = deepcopy(prices.DEFAULT_CONFIG)
        self.columns = {k: [10, 0, 100, 100] for k in ("amount", "name", "rate")}

    def read(self, row, current, monthly, bases=(), callback=None):
        calls = []
        def recognize(node, image, pipeline_override):
            roi = pipeline_override[node]["roi"]
            calls.append(roi)
            values = callback(roi) if callback else []
            return NS(filtered_results=[NS(text=t, score=s) for t, s in values])
        ctx = NS(tasker=NS(stopping=False), run_recognition=recognize)
        prices.read_prices(ctx, self.frame, row, current=current, monthly=monthly, bases=bases,
                           centers=(20, 60), columns=self.columns, cfg=self.cfg)
        return calls

    def test_consistent_initial_prices_are_not_reread(self):
        row = dict(name="正常", current_rate=117, peak_rate=120)
        calls = self.read(row, [det("5", 20)], [det("6", 60)], [det("5", 65)])
        self.assertFalse(calls)
        self.assertEqual(row["price_read_basis"], "initial_consistent")

    def test_rounding_can_give_equal_prices_at_different_rates(self):
        row = dict(name="低价", current_rate=117, peak_rate=120)
        self.read(row, [det("1", 20)], [det("1", 60)], [det("1", 65)])
        self.assertTrue(row["is_max_price"])

    def test_single_box_coin_prefix_is_logically_rejected(self):
        row = dict(name="泥炭", current_rate=103, peak_rate=120)
        self.read(row, [det("5", 20)], [det("96", 60)], [det("5", 65)],
                  callback=lambda roi: [("5" if roi[1] < 40 else "6", .99)])
        self.assertEqual(row["peak_price"], 6)
        self.assertEqual(row["price_read_basis"], "local_rescue")

    def test_two_high_score_detections_are_not_chosen_by_score(self):
        row = dict(name="小麦", current_rate=100, peak_rate=120)
        self.read(row, [det("6", 20)], [det("9", 60, .99), det("7", 60, .91)],
                  [det("6", 65)], callback=lambda roi: [("7", .85)])
        self.assertEqual(row["peak_price"], 7)

    def test_digit_count_transition_uses_wider_candidate(self):
        row = dict(name="跨位数", current_rate=100, peak_rate=120)
        calls = self.read(row, [det("99", 20)], [], [det("99", 65)],
                          callback=lambda roi: [("18" if roi[2] < 25 else "118", .99)])
        self.assertEqual(row["peak_price"], 118)
        self.assertGreater(len({r[2] for r in calls}), 1)

    def test_two_consistent_candidate_amounts_stay_unknown(self):
        row = dict(name="冲突", current_rate=100, peak_rate=120)
        self.read(row, [det("5", 20)], [], [det("5", 65)],
                  callback=lambda roi: [("5", .99), ("6", .97)])
        self.assertIsNone(row["peak_price"])
        self.assertEqual(row["max_price_basis"], "unconfirmed_amount")

    def test_missing_geometry_does_not_crop_another_row(self):
        row = dict(name="边界缺失", current_rate=100, peak_rate=120)
        prices.read_prices(NS(), self.frame, row, current=[det("5", 20)], monthly=[], bases=[],
                           centers=(20, None), columns=self.columns, cfg=self.cfg)
        self.assertEqual(row["price_read_basis"], "unconfirmed_price")

    def test_no_ocr_result_does_not_turn_formula_into_a_price(self):
        row = dict(name="空读", current_rate=100, peak_rate=120)
        self.read(row, [det("6", 20)], [], [det("6", 65)])
        self.assertIsNone(row["peak_price"])

    def test_stopping_avoids_any_new_recognition(self):
        ctx = NS(tasker=NS(stopping=True))
        self.assertEqual(prices.read_local(ctx, self.frame, prices.AMOUNT_NODE, [10, 10, 20, 20]), [])

    def test_later_good_amount_fills_missing_price_and_rechecks_peak(self):
        saved = dict(name="小麦", current_price=6, peak_price=None, current_rate=100, peak_rate=120,
                     target_cartridge="剧情游戏卡1", price_read_basis="unconfirmed_price")
        fresh = {**saved, "peak_price": 7, "price_read_basis": "initial_consistent"}
        ar._merge_cartridge_observation(saved, fresh)
        self.assertEqual(saved["peak_price"], 7)
        self.assertEqual(saved["price_read_basis"], "merged_consistent")
        ar._merge_cartridge_observation(saved, {**fresh, "peak_price": None, "price_read_basis": "unconfirmed_price"})
        self.assertEqual(saved["peak_price"], 7)

    def test_conflicting_prices_do_not_overwrite_a_known_value(self):
        saved = dict(current_price=6, peak_price=7, target_cartridge="剧情游戏卡1")
        ar._merge_cartridge_observation(saved, {**saved, "peak_price": 8})
        self.assertEqual(saved["peak_price"], 7)
        self.assertEqual(saved["price_read_basis"], "observation_conflict")

    def test_price_config_does_not_leak_between_resource_contexts(self):
        pc = prices.load_config(NS(get_node_object=lambda _: NS(attach={"white_min": 200})))
        pc["white_min"] = 240
        base = prices.load_config(NS(get_node_object=lambda _: NS(attach={})))
        self.assertEqual(base, prices.DEFAULT_CONFIG)


class CartridgeEntryTests(unittest.TestCase):
    def test_legal_but_low_quality_one_enters_existing_rescue(self):
        typed = dict(text="角色遊戲卡帶", x=913, y=309, w=67, h=14, cx=946.5, cy=316, score=.95)
        number = dict(text="1", x=970, y=319, w=9, h=12, cx=974.5, cy=325, score=.721)
        calls = []
        def recognize(node, image, pipeline_override):
            roi = pipeline_override[node]["roi"]
            calls.append(roi)
            return NS(filtered_results=[NS(text="7", score=.9, box=roi)])
        result = cart.read_current([typed, number], NS(run_recognition=recognize), object(),
                                   cart.DEFAULT_CONFIG, (908, 304, 990, 342), "水果冰沙")
        self.assertTrue(calls)
        self.assertEqual(result[0], "角色游戏卡7")


if __name__ == "__main__":
    unittest.main()
