"""价目表「当前」标签切格回归；只用内存读数，不连接游戏或写存档。

运行：python -B tools/verify_arbitrage_price_grid.py -v
"""

from pathlib import Path
import re
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from action import arbitrage_result as ar
from utils import arbitrage_price_grid as grid
from utils import arbitrage_pricelist as pricelist
from verify_arbitrage_cartridge import Reader, det

PATTERNS = {key: re.compile(value) for key, value in grid.DEFAULT_PATTERNS.items()}


def label(text, y, h=17):
    return det(text, 730, y, 40, h)


class GridTests(unittest.TestCase):
    def test_lines_run_through_the_middle_of_each_gap(self):
        rows, problem = grid.draw_rows([label("当前", 240), label("每月6日", 280),
                                        label("当前", 328), label("每月9日", 368)], 209, 553, PATTERNS)
        self.assertIsNone(problem)
        # 中位线 (240+17+280)/2；大行线 (280+17+328)/2——上框下边与下框上边取中点
        # 首行上方没有框：「当前」到中位线的 11.5 对称上推
        self.assertEqual([(r["top"], r["mid"], r["bottom"]) for r in rows],
                         [(228.5, 268.5, 312.5), (312.5, 356.5, 553)])

    def test_monthly_text_is_not_needed_and_misread_current_is_tolerated(self):
        rows, problem = grid.draw_rows([label("营前", 240), label("??", 280)], 209, 553, PATTERNS)
        self.assertIsNone(problem)
        self.assertEqual(rows[0]["monthly"]["text"], "??")

    def test_structure_problems_are_reported_not_guessed(self):
        cases = {
            "no_current": [label("每月6日", 280)],
            "labels_not_alternating:1": [label("当前", 240), label("当前", 328), label("每月9日", 368)],
        }
        for problem, labels in cases.items():
            with self.subTest(problem=problem):
                self.assertEqual(grid.draw_rows(labels, 209, 553, PATTERNS), ([], problem))
        rows, problem = grid.draw_rows([label("当前", 240), label("每月6日", 268), label("x", 284)],
                                       209, 553, PATTERNS)
        self.assertEqual((rows, problem), ([], "labels_not_alternating:1"))

    def test_any_box_above_the_first_current_draws_the_first_line_without_reading_it(self):
        for above, bottom_edge in ((label("最高价", 210, 21), 231), (label("月6日", 209, 9), 218),
                                   (label("??", 209, 9), 218)):
            with self.subTest(text=above["text"]):
                rows, _ = grid.draw_rows([above, label("当前", 240), label("每月6日", 280)], 209, 553, PATTERNS)
                self.assertEqual(rows[0]["top"], (bottom_edge + 240) / 2)

    def test_last_row_cut_at_the_bottom_keeps_a_symmetric_upper_cell_only(self):
        rows, problem = grid.draw_rows([label("当前", 240), label("每月6日", 280), label("当前", 328)],
                                       209, 553, PATTERNS)
        self.assertIsNone(problem)
        last = rows[-1]
        self.assertIsNone(last["mid"])
        # 上沿 312.5 到「当前」上边 328 的距离，原样放到「当前」下边 345 之下
        self.assertEqual(last["upper_end"], 345 + (328 - 312.5))

    def test_base_cell_drops_every_non_digit(self):
        dets = grid.digits_only([det("è5", 484, 518, 34, 20), det("XT", 484, 520, 30, 9), det("1.875", 480, 520, 50, 20)])
        self.assertEqual([d["text"] for d in dets], ["5", "1875"])


class PriceReader(Reader):
    """Reader plus name/amount rescue reads; unknown nodes read nothing."""

    def __init__(self, columns, name_reads=()):
        super().__init__(columns)
        self.name_reads = list(name_reads)

    def run_recognition(self, node, image, pipeline_override=None):
        if node == pricelist.NAME_NODE:
            return NS(filtered_results=[NS(text=t, score=.99, box=[0, 0, 1, 1]) for t in self.name_reads])
        if node == pricelist.AMOUNT_NODE:
            return NS(filtered_results=[])
        return super().run_recognition(node, image, pipeline_override)


def page(names, rows=None, label_boxes=None):
    """One base-layout item per entry in rows: (current label y, monthly label y)."""
    rows = rows or [(489, 524)]
    labels = label_boxes if label_boxes is not None else [
        box for cur, mon in rows for box in (label("当前", cur), label("每月27日", mon))]
    amounts, prices, carts = [], [], []
    for cur, mon in rows:
        amounts += [det("5", 845, cur - 3, 30, 24), det("6", 845, mon - 3, 30, 24)]
        prices += [det("100%", 898, cur - 3, 60, 24), det("120%", 898, mon - 3, 60, 24)]
        carts += [det("剧情游戏卡14", 966, cur, 83, 17), det("剧情游戏卡3", 966, mon, 83, 17)]
    return {ar._COL_LABEL: labels, ar._COL_NAME: names, ar._COL_AMOUNT: amounts,
            ar._COL_PRICE: prices, ar._COL_CART: carts}


def parse(columns, name_reads=()):
    return ar.ArbitrageSellController()._parse_current_page(PriceReader(columns, name_reads))


class ParserTests(unittest.TestCase):
    def test_pr536_coin_glyph_on_the_base_line_is_not_an_item(self):
        # PR #536 作者样本：名称中心 496.5，图标 `è` 中心 527.5（0.577），同排数字 5
        rows = parse(page([det("兽肉", 478, 485, 40, 23), det("è", 484, 518, 18, 19),
                           {**det("5", 497, 517, 21, 21), "score": .999932}]))
        self.assertEqual([r["name"] for r in rows], ["兽肉"])
        self.assertTrue(rows[0]["name_confirmed"])
        self.assertEqual((rows[0]["current_price"], rows[0]["peak_price"]), (5, 6))
        self.assertEqual((rows[0]["current_rate"], rows[0]["peak_rate"]), (100, 120))
        self.assertEqual(rows[0]["price_read_basis"], "initial_consistent")
        self.assertEqual(rows[0]["base_price"], 5)

    def test_glued_coin_and_base_price_still_reads_the_base(self):
        rows = parse(page([det("兽肉", 478, 485, 40, 23), det("è5", 484, 517, 34, 21)]))
        self.assertEqual([r["name"] for r in rows], ["兽肉"])
        self.assertEqual(rows[0]["base_price"], 5)

    def test_leftover_half_row_on_top_never_joins_the_first_name(self):
        labels = [label("月6日", 209, 9), label("当前", 489), label("每月27日", 524)]
        rows = parse(page([det("XT", 484, 209, 54, 9), det("流浪美食家烤肉", 478, 485, 110, 23)],
                          label_boxes=labels))
        self.assertEqual([r["name"] for r in rows], ["流浪美食家烤肉"])

    def test_name_split_into_two_boxes_is_joined_left_to_right(self):
        rows = parse(page([det("鲜血布丁", 548, 486, 70, 22), det("三分熟", 478, 485, 60, 23)]))
        self.assertEqual(rows[0]["name"], "三分熟鲜血布丁")
        self.assertTrue(rows[0]["name_confirmed"])

    def test_empty_name_cell_is_kept_as_an_unconfirmed_item(self):
        rows = parse(page([det("5", 497, 517, 21, 21)]))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["name_confirmed"])
        self.assertTrue(rows[0]["name"].startswith("未读出名称@"))

    def test_empty_name_cell_can_be_recovered_by_the_cell_reread(self):
        rows = parse(page([]), name_reads=["兽肉"])
        self.assertEqual(rows[0]["name"], "兽肉")
        self.assertTrue(rows[0]["name_confirmed"])

    def test_page_without_labels_or_with_broken_labels_yields_nothing(self):
        names = [det("兽肉", 478, 485, 40, 23)]
        self.assertEqual(parse(page(names, label_boxes=[])), [])
        self.assertEqual(parse(page(names, label_boxes=[label("当前", 489), label("当前", 524)])), [])

    def test_bottom_row_without_monthly_is_not_judged_at_peak(self):
        labels = [label("当前", 401), label("每月6日", 436), label("当前", 489)]
        rows = parse(page([det("大蒜", 478, 397, 44, 23), det("兽肉", 478, 485, 40, 23)],
                          rows=[(401, 436), (489, 524)], label_boxes=labels))
        self.assertEqual([r["name"] for r in rows], ["大蒜", "兽肉"])
        self.assertEqual(rows[1]["price_read_basis"], "unconfirmed_price")
        self.assertFalse(rows[1]["is_max_price"])
        # 上格完整，今天的卡带照读；被截断的月度卡带不采信
        self.assertEqual(rows[1]["target_cartridge"], "剧情游戏卡14")
        self.assertEqual(rows[1]["monthly_cartridge"], "")


if __name__ == "__main__":
    unittest.main()
