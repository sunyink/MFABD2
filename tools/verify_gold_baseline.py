"""金币基准轮次回归：不连接游戏、不读写账号存档。"""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from action import gold_verify as gv


class GoldBaselineTests(unittest.TestCase):
    def setUp(self):
        gv.clear_verdict()
        self.argv = SimpleNamespace(custom_action_param="")
        self.snapshot = gv.GoldSnapshot()
        self.verdict = gv.GoldVerdict()

    def tearDown(self):
        gv.clear_verdict()

    def test_repeated_snapshot_keeps_first_read_including_zero_gold(self):
        for initial in (0, 1000):
            gv.clear_verdict()
            with self.subTest(initial=initial), patch.object(gv, "_read_gold", side_effect=[initial, initial + 200]) as read:
                self.assertTrue(self.snapshot.run(None, self.argv))
                self.assertTrue(self.snapshot.run(None, self.argv))
                self.assertTrue(self.verdict.run(None, self.argv))
            self.assertEqual(read.call_count, 2)
            self.assertEqual(gv.take_verdict(), {"before": initial, "after": initial + 200, "delta": 200})

    def test_next_dispatch_records_a_new_baseline(self):
        for before, after in ((1000, 1200), (1200, 1500)):
            gv.clear_verdict()
            with patch.object(gv, "_read_gold", side_effect=[before, after]):
                self.snapshot.run(None, self.argv)
                self.verdict.run(None, self.argv)
            self.assertEqual(gv.take_verdict(), {"before": before, "after": after, "delta": after - before})
            self.assertIsNone(gv.take_verdict())

    def test_unreadable_initial_gold_is_not_replaced_by_later_gold(self):
        with patch.object(gv, "_read_gold", side_effect=[None, 1300]) as read:
            self.snapshot.run(None, self.argv)
            self.snapshot.run(None, self.argv)
            self.verdict.run(None, self.argv)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(gv.take_verdict(), {"before": None, "after": 1300, "delta": None})

    def test_snapshot_after_verdict_cannot_overwrite_completed_measurement(self):
        with patch.object(gv, "_read_gold", side_effect=[1000, 1200, 1200]) as read:
            self.snapshot.run(None, self.argv)
            self.verdict.run(None, self.argv)
            self.snapshot.run(None, self.argv)
            self.verdict.run(None, self.argv)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(gv.take_verdict(), {"before": 1000, "after": 1200, "delta": 200})

    def test_aborted_dispatch_cannot_leak_its_baseline_into_the_next(self):
        with patch.object(gv, "_read_gold", return_value=1000):
            self.snapshot.run(None, self.argv)
        self.assertIsNone(gv.take_verdict())
        gv.clear_verdict()
        with patch.object(gv, "_read_gold", side_effect=[2000, 2500]):
            self.snapshot.run(None, self.argv)
            self.verdict.run(None, self.argv)
        self.assertEqual(gv.take_verdict(), {"before": 2000, "after": 2500, "delta": 500})

    def test_snapshot_is_serial_and_not_limited_by_framework_hit_count(self):
        pipeline = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        self.assertEqual(pipeline["Arbitrage_Sell_Item_ListTraverse"]["next"], ["Arbitrage_Sell_Gold_Snapshot"])
        node = pipeline["Arbitrage_Sell_Gold_Snapshot"]
        self.assertNotIn("max_hit", node)
        self.assertEqual(node["next"], ["Arbitrage_Sell_Item_Click"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
