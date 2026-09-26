"""背包详情名称容错接入回归；模拟 OCR 读数，不连接游戏或读写存档。"""

from functools import partial
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from action import bag_stock as bag
from utils.ocr_item_name import resolve_ocr_name


class BagNameTests(unittest.TestCase):
    def inspect(self, target, observations, quantity="持有81,861個"):
        frames = iter(observations)
        last = observations[-1]
        calls = []
        def recognize(node, image, override):
            nonlocal last
            self.assertEqual(node, "name")
            last = next(frames, last)
            calls.append(last)
            return NS(hit=bool(last), filtered_results=[NS(text=text, score=score) for text, score in last])
        context = NS(tasker=NS(stopping=False), run_recognition=recognize)
        scanner = bag.BagScanner(context, {"timeout_seconds": 10, "name_node": "name",
                                          "quantity_node": "quantity", "click_node": "click",
                                          "close_node": "close"})
        scanner.capture = Mock(side_effect=lambda: object())
        scanner.action = Mock()
        scanner.list_image = Mock()
        with patch.object(bag, "_read_texts", return_value=([quantity], 1, True)) as read_quantity:
            result = scanner.inspect(target, [1, 2, 3, 4])
        self.assertEqual(scanner.action.call_args_list[-1].args[0], "close")
        scanner.list_image.assert_called_once()
        self.assertLessEqual(len(calls), 6)
        return result, calls, read_quantity

    def test_recorded_seaweed_spelling_reaches_quantity_without_reread(self):
        result, calls, quantity = self.inspect("调味海苔", [[("包装好的海苔", .990409)]])
        self.assertEqual(result, 81861)
        self.assertEqual(len(calls), 1)
        quantity.assert_called_once()

    def test_exact_traditional_name_still_accepts_zero(self):
        result, _, _ = self.inspect("调味海苔", [[("包裝好的海苔", .99)]], "持有0個")
        self.assertEqual(result, 0)

    def test_low_confidence_requires_good_reread(self):
        result, calls, _ = self.inspect("调味海苔", [[("包装好的海苔", .4)],
                                                 [("包裝好的海苔", .99)], [("包裝好的海苔", .98)]])
        self.assertEqual(result, 81861)
        self.assertEqual(len(calls), 3)
        result, _, quantity = self.inspect("调味海苔", [[("包装好的海苔", .4)]])
        self.assertIsNone(result)
        quantity.assert_not_called()

    def test_known_other_item_and_unknown_text_cannot_supply_target_quantity(self):
        for observations in ([[("牛奶", .99)]], [[("完全未知的新商品", .99)]], [[]],
                             [[("包裝好的海苔", .99), ("牛奶", .99)]]):
            with self.subTest(observations=observations):
                result, _, quantity = self.inspect("调味海苔", observations)
                self.assertIsNone(result)
                quantity.assert_not_called()

    def test_ambiguous_name_is_not_resolved_using_the_requested_target(self):
        resolver = partial(resolve_ocr_name, aliases={"香甜面包": "巧克力面包", "香甜蛋糕": "手工蛋糕"})
        with patch.object(bag, "resolve_ocr_name", side_effect=resolver):
            result, _, quantity = self.inspect("巧克力面包", [[("香甜面糕", .99)]])
        self.assertIsNone(result)
        quantity.assert_not_called()

    def test_short_fuzzy_name_keeps_two_reread_requirement(self):
        result, calls, _ = self.inspect("胶合板", [[("夹板", .99)]])
        self.assertEqual(result, 81861)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
