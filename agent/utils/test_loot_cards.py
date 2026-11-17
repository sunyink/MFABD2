"""loot_cards（吸取掉落按卡带落盘）的状态单测：不依赖 MaaFramework，也不写存档。

跑法（都从仓库根执行）：
    python agent/utils/test_loot_cards.py
    python -m unittest discover -s agent/utils -p 'test_loot_cards.py'
"""

import unittest

try:
    from . import loot_cards as lc
except ImportError:      # 直接 python agent/utils/test_loot_cards.py
    import loot_cards as lc

T1 = {"entry": "Collect_StartGame_HomePage", "task_id": 1}
T2 = {"entry": "Collect_StartGame_HomePage", "task_id": 2}
A, B = "Story_01_血骑士", "Story_02_苍蓝魔女"


def point(t, *items, unread=0, stuck=False):
    return {"time": t, "items": [{"name": name, "count": count, "known": known} for name, count, known in items],
            "unread": unread, "stuck": stuck}


class LootCardsTests(unittest.TestCase):
    def setUp(self):
        lc.reset()

    def test_complete_hands_over_the_card_with_totals(self):
        self.assertIsNone(lc.enter(A, task=T1, session="s", account="acc", now=100))
        lc.add_point(point(110, ("兽肉", 30, True), ("金币", 500, True)), card=A, task=T1, now=110)
        lc.add_point(point(150, ("兽肉", 20, True), ("怪东西", 1, False), unread=1, stuck=True), card=A, task=T1, now=150)
        voucher = lc.leave("complete", task_id=1, card=A, now=160)
        self.assertEqual((voucher["card"], voucher["end"], voucher["points"]), (A, "complete", 2))
        self.assertEqual(voucher["items"], {"兽肉": 50, "金币": 500})
        self.assertEqual((voucher["unknown"], voucher["unread"], voucher["stuck"]), ({"怪东西": 1}, 1, 1))
        self.assertEqual((voucher["first_at"], voucher["last_at"]), (lc._iso(110), lc._iso(150)))
        self.assertEqual((voucher["entered_at"], voucher["account"], voucher["session"]), (lc._iso(100), "acc", "s"))
        self.assertIsNone(lc.current())

    def test_next_card_name_hands_over_the_previous_one(self):
        lc.enter(A, task=T1, now=100)
        lc.add_point(point(110, ("兽肉", 30, True)), card=A, task=T1)
        self.assertIsNone(lc.enter(A, task=T1, now=120))           # 同一张卡、同一个任务：继续记
        voucher = lc.enter(B, task=T1, now=200)                    # 卡内失败后进了下一张
        self.assertEqual((voucher["card"], voucher["end"], voucher["items"]), (A, "switched", {"兽肉": 30}))
        self.assertEqual(lc.current()["card"], B)

    def test_visit_without_points_is_not_written(self):
        lc.enter(A, task=T1, now=100)                              # 没开记录：只有卡名
        self.assertIsNone(lc.enter(B, task=T1, now=200))
        self.assertIsNone(lc.leave("complete", task_id=1, now=300))

    def test_stopped_task_is_handed_over_later(self):
        lc.enter(A, task=T1, now=100)
        lc.add_point(point(110, ("兽肉", 30, True)), card=A, task=T1)
        self.assertEqual(lc.enter(A, task=T2, now=500)["end"], "later")   # 中途停了，下个任务又进同一张卡

    def test_point_with_a_different_card_splits_and_missing_name_is_filled(self):
        lc.add_point(point(110, ("兽肉", 5, True)), card=None, task=T1)    # 没赶上进卡、卡名读不到
        lc.add_point(point(120, ("兽肉", 5, True)), card=A, task=T1)       # 读到了：补上卡名
        self.assertEqual(lc.current()["card"], A)
        voucher = lc.add_point(point(130, ("兽肉", 7, True)), card=B, task=T1)
        self.assertEqual((voucher["card"], voucher["end"], voucher["items"]), (A, "switched", {"兽肉": 10}))
        self.assertIsNone(lc.add_point(point(140, ("兽肉", 1, True)), card=None, task=T1))   # 读不到就并进当前这次
        self.assertEqual(len(lc.current()["points"]), 2)

    def test_complete_for_another_card_is_recorded_as_switched(self):
        lc.enter(A, task=T1, now=100)
        lc.add_point(point(110, ("兽肉", 30, True)), card=A, task=T1)
        self.assertEqual(lc.leave("complete", task_id=1, card=B, now=200)["end"], "switched")

    def test_settle_at_ledger_positions(self):
        lc.enter(A, task=T1, now=100)
        lc.add_point(point(110, ("兽肉", 30, True)), card=A, task=T1)
        self.assertIsNone(lc.settle(1, task_end=False))            # 本任务的开头之类：正在记的不动
        self.assertEqual(lc.settle(1, task_end=True)["end"], "task_end")
        lc.enter(A, task=T1, now=300)
        lc.add_point(point(310, ("兽肉", 3, True)), card=A, task=T1)
        self.assertEqual(lc.settle(2, task_end=False)["end"], "later")   # 下一个任务的记账位置替它交
        lc.enter(A, task=T1, now=400)
        lc.add_point(point(410, ("兽肉", 3, True)), card=A, task=T1)
        self.assertEqual(lc.settle(2, task_end=True)["end"], "later")    # 次数一开始就满的任务只替前面收尾

    def test_store_refuses_another_account(self):
        lc.enter(A, task=T1, account="acc", now=100)
        lc.add_point(point(110, ("兽肉", 30, True)), card=A, task=T1)
        voucher = lc.leave("complete", task_id=1, now=200)
        self.assertIn("另一个账号", lc.store(voucher, "other"))
        self.assertIsNone(lc.store(None, "acc"))

    def test_bad_end_is_rejected(self):
        with self.assertRaises(ValueError):
            lc.leave("done")


if __name__ == "__main__":
    unittest.main()
