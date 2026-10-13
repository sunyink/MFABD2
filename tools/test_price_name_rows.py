"""Price-row glyph regressions; no game connection or persistent state."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'agent'))
from utils.arbitrage_name_rows import is_price_subrow_noise


class PriceNameRows(unittest.TestCase):
    def setUp(self):
        self.token = dict(text='è', x=484, w=18, cy=527.5, score=.577191)
        self.number = dict(text='5', x=497, w=21, cy=527.5, score=.999932)
        self.anchors = [dict(cy=496.5, identity=dict(confirmed=True))]

    def test_observed_price_line_glyph_is_not_a_product(self):
        self.assertTrue(is_price_subrow_noise(self.token, [self.number], self.anchors))

    def test_chinese_truncations_real_words_and_confident_text_stay_unknown(self):
        for text in ['茶', '盐', '兽', 'è茶', 'tea']:
            self.assertFalse(is_price_subrow_noise({**self.token, 'text':text}, [self.number], self.anchors))
        self.assertFalse(is_price_subrow_noise({**self.token, 'score':.6}, [self.number], self.anchors))

    def test_product_baseline_first_row_and_unconfirmed_predecessor_are_not_suppressed(self):
        self.assertFalse(is_price_subrow_noise({**self.token, 'cy':496.5}, [self.number], self.anchors))
        self.assertFalse(is_price_subrow_noise({**self.token, 'cy':586.5}, [self.number], self.anchors))
        self.assertFalse(is_price_subrow_noise(self.token, [self.number], []))
        self.assertFalse(is_price_subrow_noise(self.token, [self.number],
            [dict(cy=496.5, identity=dict(confirmed=False))]))

    def test_missing_far_weak_or_misaligned_price_number_cannot_suppress(self):
        self.assertFalse(is_price_subrow_noise(self.token, [], self.anchors))
        for delta in [dict(x=900), dict(score=.5), dict(cy=550)]:
            self.assertFalse(is_price_subrow_noise(self.token, [{**self.number, **delta}], self.anchors))


if __name__ == '__main__':
    unittest.main()
