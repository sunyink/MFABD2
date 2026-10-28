"""背包详情名称容错接入回归；模拟 OCR 读数，不连接游戏或读写存档。

名称一律对完整名录核对：读出来是哪个物品就记哪个物品，不拿「本来想找的目标」去缩小候选。
"""

from functools import partial
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from utils import bag_scan_engine as engine_module
from utils.bag_scan_engine import BagScanEngine, Reading
from utils.ocr_item_name import resolve_ocr_name

BOX = [270, 263, 90, 24]


class Device:
    """只提供读数相关的几样操作；数量帧固定、名称重读按给定序列返回。"""

    def __init__(self, rereads, quantity):
        self.rereads = iter(rereads)
        self.quantity = quantity
        self.name_calls = 0

    def stopping(self):
        return False

    def read_names(self):
        self.name_calls += 1
        return next(self.rereads, [])

    def read_detail(self):
        return Reading(names=[], quantities=[(self.quantity, .99, list(BOX))])

    def read_quantity_scaled(self):
        return [self.quantity]


class BagNameTests(unittest.TestCase):
    def read(self, observations, quantity="持有81,861個".replace(",", "")):
        first, *rereads = observations
        device = Device(rereads, quantity)
        engine = BagScanEngine(device, {}, [], grid_roi=[0, 0, 10, 10], row_height=100, ingredient_names=())
        # 复核帧的名称为空时，要靠首帧原文对上；这里让复核帧带上首帧原文。
        device.read_detail = lambda: Reading(names=list(first),
                                            quantities=[(quantity, .99, list(BOX))])
        name, value = engine._read(Reading(names=list(first), quantities=[(quantity, .99, list(BOX))]))
        return name, value, device.name_calls

    def test_recorded_seaweed_spelling_reaches_quantity_without_reread(self):
        name, value, calls = self.read([[("包装好的海苔", .990409)]])
        self.assertEqual((name, value, calls), ("调味海苔", 81861, 0))

    def test_exact_traditional_name_still_accepts_zero(self):
        name, value, _ = self.read([[("包裝好的海苔", .99)]], "持有0個")
        self.assertEqual((name, value), ("调味海苔", 0))

    def test_low_confidence_requires_good_reread(self):
        name, value, calls = self.read([[("包装好的海苔", .4)], [("包裝好的海苔", .99)], [("包裝好的海苔", .98)]])
        self.assertEqual((name, value, calls), ("调味海苔", 81861, 2))
        name, value, _ = self.read([[("包装好的海苔", .4)]])
        self.assertEqual((name, value), (None, None))

    def test_other_known_item_is_recorded_as_itself_and_unknown_text_is_not_read(self):
        name, value, _ = self.read([[("牛奶", .99)]])
        self.assertEqual((name, value), ("牛奶", 81861))
        for observations in ([[("完全未知的新商品", .99)]], [[]], [[("包裝好的海苔", .99), ("牛奶", .99)]]):
            with self.subTest(observations=observations):
                self.assertEqual(self.read(observations)[:2], (None, None))

    def test_ambiguous_name_is_not_resolved(self):
        resolver = partial(resolve_ocr_name, aliases={"香甜面包": "巧克力面包", "香甜蛋糕": "手工蛋糕"})
        with patch.object(engine_module, "resolve_ocr_name", side_effect=resolver):
            self.assertEqual(self.read([[("香甜面糕", .99)]])[:2], (None, None))

    def test_short_fuzzy_name_keeps_two_reread_requirement(self):
        name, value, calls = self.read([[("夹板", .99)], [("夹板", .99)], [("夹板", .99)]])
        self.assertEqual((name, value, calls), ("胶合板", 81861, 2))


if __name__ == "__main__":
    unittest.main()
