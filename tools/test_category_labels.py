"""Offline sale-list heading regression; no device, transactions or save writes."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'agent'))
from utils.ocr_item_name import is_category_label, resolve_ocr_name


class CategoryLabelsTests(unittest.TestCase):
    def test_native_decorated_heading(self):
        for text in ['◆食物', '◇料理食材', '■裝備材料', '●圣石', '◆ 食物', '食物']:
            with self.subTest(text=text):
                self.assertTrue(is_category_label(text))

    def test_item_names_are_not_headings(self):
        for text in ['茶', '藏红花甜茶', '蘑菇汤', '料理食材汤', '◆蘑菇汤', '食物残片', '']:
            with self.subTest(text=text):
                self.assertFalse(is_category_label(text))

    def test_truncated_name_still_blocks_zero_inventory(self):
        result = resolve_ocr_name('茶', aliases={'藏红花甜茶': '藏红花甜茶'})
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['basis'], 'unconfirmed_name')


if __name__ == '__main__':
    unittest.main()
