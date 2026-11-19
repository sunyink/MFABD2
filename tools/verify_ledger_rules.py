"""库存记账运行时判定回归：流程走到一个位置时盘不盘、盘哪几类。

只测 agent/utils/ledger_rules.py 的纯函数：用假的任务起止与假的盘点结果按剧本走一遍，不需要 maa、不连游戏。直接运行：
    python -B tools/verify_ledger_rules.py -v
"""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import ledger_rules  # noqa: E402
from utils.ledger_rules import ALL_SCOPES, LedgerState, decide, game_day, settings, settle  # noqa: E402

QUIET = ("Env_MultiSave_Config", "StartGame_Start", "Env_Ledger_Config",
         "Collect_StartGame_HomePage", "Arbitrage_Start", "Close_HomePage", "Ledger_Settle_Manual")
FULL = list(ALL_SCOPES)
NOON = 1_763_208_000.0   # 2025-11-15 12:00 UTC，离游戏日分界远


class Play:
    """按剧本走：task() 开一个任务，at() 到一个位置。每次盘点花 100 秒。"""

    def __init__(self, start=NOON):
        self.state, self.rows, self.now, self.next_id = LedgerState(), [], start, 100
        self.log = []

    def task(self, entry, gap=3.0):
        if self.rows and self.rows[-1]["end"] is None:
            self.rows[-1]["end"] = self.now
        self.now += gap
        self.next_id += 1
        self.rows.append({"entry": entry, "task_id": self.next_id, "start": self.now, "end": None})
        self.now += 20
        return self

    def at(self, mark, kind=None, executed=True):
        row = self.rows[-1]
        decision = decide(mark, kind, self.state, task_id=row["task_id"], rows=self.rows, now=self.now, quiet=QUIET)
        scan = None
        if decision["scan"]:
            self.now += 100
            if executed:
                scan = {"scan_id": f"s{len(self.log)}", "scopes": decision["scopes"]}
        settle(self.state, mark=mark, kind=kind, task_id=row["task_id"], entry=row["entry"], now=self.now, scan=scan)
        self.log.append(decision)
        self.now += 30
        return decision

    def daily(self, entry="QuickHunt_Start", **kwargs):
        return self.task(entry, **kwargs).at("begin", "daily")

    def scans(self):
        return [item["scopes"] for item in self.log if item["scan"]]


class LedgerRuleTests(unittest.TestCase):
    def test_k1_preset_order_scans_four_times(self):
        play = Play()
        for entry in ("QuickHunt_Start", "Equip_HomePage", "Mail_HomePage"):
            play.daily(entry)
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        ended = play.at("end", "collect")
        shop_begin = play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        close = play.task("Close_HomePage").at("queue_end")
        self.assertEqual(play.scans(), [FULL, FULL, ["A"], ["A"]])
        self.assertTrue(ended["end_mark"])
        self.assertFalse(shop_begin["scan"])
        self.assertFalse(close["scan"])

    def test_k2_daily_tasks_in_a_row_scan_once(self):
        play = Play()
        for entry in ("Mail_HomePage", "PVP_TaskStart", "Weekly_Start", "Daily_HomePage"):
            play.daily(entry)
        self.assertEqual(play.scans(), [FULL])

    def test_k4_collect_without_normal_end_is_closed_at_shop_begin(self):
        play = Play()
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        decision = play.task("Arbitrage_Start").at("begin", "shop")
        self.assertEqual(decision["scopes"], ["A"])

    def test_k5_daily_after_shop_rescans_what_shop_did_not_cover(self):
        play = Play()
        play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        self.assertEqual(play.daily("Mail_HomePage")["scopes"], ["B", "C"])
        self.assertEqual(play.task("Close_HomePage").at("queue_end")["scopes"], ["B", "C"])

    def test_k6_collect_between_two_daily_tasks(self):
        play = Play()
        play.daily("Mail_HomePage")
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        play.at("end", "collect")
        play.daily("Pass_HomePage")
        play.task("Close_HomePage").at("queue_end")
        self.assertEqual(play.scans(), [FULL, FULL, ["A"], ["B", "C"], ["B", "C"]])

    def test_k7_collect_already_depleted_closes_daily_without_end_mark(self):
        play = Play()
        play.daily("Mail_HomePage")
        decision = play.task("Collect_StartGame_HomePage").at("end", "collect")
        self.assertEqual(decision["scopes"], ["B", "C"])
        self.assertFalse(decision["end_mark"])

    def test_k8_task_that_never_reaches_a_point_is_ignored(self):
        play = Play()
        play.daily("Mail_HomePage")
        play.task("Arbitrage_Start")                      # 五个阶段全关：走不到位置 A
        self.assertFalse(play.daily("Pass_HomePage")["scan"])

    def test_k9_shop_stopped_midway_is_closed_by_next_daily(self):
        play = Play()
        play.task("Arbitrage_Start").at("begin", "shop")
        self.assertEqual(play.daily("Mail_HomePage")["scopes"], FULL)

    def test_k10_same_type_again_after_abnormal_end_does_not_rescan(self):
        play = Play()
        play.task("Arbitrage_Start").at("begin", "shop")
        again = play.task("Arbitrage_Start").at("begin", "shop")
        ended = play.at("end", "shop")
        self.assertFalse(again["scan"])
        self.assertEqual(ended["scopes"], ["A"])
        self.assertTrue(ended["end_mark"])

    def test_k11_gap_or_new_process_means_full_first_scan(self):
        play = Play()
        play.daily("Mail_HomePage")
        self.assertEqual(play.daily("Pass_HomePage", gap=600)["scopes"], FULL)
        fresh = Play()                                     # Agent 重启：内存状态没了
        self.assertEqual(fresh.daily("Pass_HomePage")["scopes"], FULL)

    def test_k12_unwired_task_in_between_forces_rescan(self):
        play = Play()
        play.daily("Mail_HomePage")
        play.task("Fishing_Entry")
        decision = play.daily("Pass_HomePage")
        self.assertEqual(decision["scopes"], FULL)
        self.assertIn("Fishing_Entry", decision["reason"])

    def test_k13_inventory_neutral_tasks_do_not_count(self):
        play = Play()
        play.daily("Mail_HomePage")
        play.task("Env_MultiSave_Config")
        play.task("StartGame_Start")
        self.assertFalse(play.daily("Pass_HomePage")["scan"])

    def test_k14_daily_crossing_game_day_rescans(self):
        play = Play(start=NOON + 12 * 3600 - 400)          # 离 UTC 零点（09:00 KST）不到 7 分钟
        play.daily("Mail_HomePage")
        play.daily("Pass_HomePage")                        # 仍是同一天
        self.assertNotEqual(game_day(play.now), game_day(play.now + 300))
        play.now += 300
        self.assertEqual(play.daily("Weekly_Start")["scopes"], FULL)
        self.assertEqual(play.scans(), [FULL, FULL])

    def test_closing_position_crossing_game_day_scans_everything(self):
        play = Play(start=NOON + 12 * 3600 - 400)          # 08:53 KST：日常、吸取开头都在换日前
        play.daily("Mail_HomePage")
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        play.now += 600                                    # 吸取跑过 09:00 KST
        crossing = play.at("end", "collect")
        self.assertEqual((crossing["scopes"], crossing["reason"]), (FULL, "收尾，跨过游戏日"))
        self.assertFalse(play.task("Arbitrage_Start").at("begin", "shop")["scan"])   # 同一天里照旧一盘两用
        self.assertEqual(play.at("end", "shop")["scopes"], ["A"])                    # 不再跨日：只盘本阶段要的
        self.assertEqual(play.scans(), [FULL, FULL, FULL, ["A"]])

    def test_closing_position_crossing_game_day_without_activity_does_not_scan(self):
        play = Play(start=NOON + 12 * 3600 - 400)
        play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        play.now += 600
        self.assertFalse(play.task("Close_HomePage").at("queue_end")["scan"])   # 没有未盘掉的活动：留给下一个开头去全盘

    def test_k15_scan_that_did_not_run_leaves_activity_pending(self):
        play = Play()
        first = play.task("Mail_HomePage").at("begin", "daily", executed=False)
        self.assertTrue(first["scan"])
        self.assertIsNone(play.state.last_scan)
        self.assertEqual(play.daily("Pass_HomePage")["scopes"], FULL)   # 仍没有可接的盘点：再按第一盘
        play.task("Collect_StartGame_HomePage").at("begin", "collect", executed=False)
        self.assertEqual([item["kind"] for item in play.state.pending], ["daily", "collect"])
        self.assertEqual(play.at("end", "collect")["scopes"], FULL)     # 日常与吸取都还没盘掉

    def test_k17_accounts_keep_separate_state(self):
        one, two = Play(), Play()
        one.daily("Mail_HomePage")
        self.assertEqual(two.daily("Mail_HomePage")["scopes"], FULL)
        self.assertFalse(one.daily("Pass_HomePage")["scan"])

    def test_k18_manual_settle(self):
        play = Play()
        self.assertEqual(play.task("Ledger_Settle_Manual").at("manual")["scopes"], FULL)   # 单独跑：盘一次
        self.assertFalse(play.task("Ledger_Settle_Manual").at("manual")["scan"])           # 紧接着再跑：不盘
        play.daily("Mail_HomePage")
        self.assertEqual(play.task("Ledger_Settle_Manual").at("manual")["scopes"], ["B", "C"])

    def test_k19_close_right_after_shop_end_does_not_scan(self):
        play = Play()
        play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        self.assertFalse(play.task("Close_HomePage").at("queue_end")["scan"])
        lonely = Play()                                    # 单独跑结束游戏：没有活动，不盘
        self.assertFalse(lonely.task("Close_HomePage").at("queue_end")["scan"])

    def test_two_segments_inside_one_task(self):
        play = Play()
        play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        again = play.at("begin", "shop")                   # 同一任务里的第二段
        self.assertFalse(again["scan"])
        self.assertEqual(play.at("end", "shop")["scopes"], ["A"])

    def test_settings_come_from_the_node_and_fall_back_item_by_item(self):
        """免记名单与连续阈值以节点 Agt_Ledger_Config.attach 为准；内置值只是节点读不到时的替身，两边要一致。"""
        built_in = {"quiet": ledger_rules.QUIET, "gap": ledger_rules.GAP_SECONDS}
        nodes = json.loads((ROOT / "assets/resource/base/pipeline/Ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(settings(nodes["Agt_Ledger_Config"]["attach"]), built_in)
        self.assertEqual(settings({"quiet": ["甲", "乙"], "gap_seconds": 5}), {"quiet": ("甲", "乙"), "gap": 5.0})
        self.assertEqual(settings({"quiet": ["甲"]}), {"quiet": ("甲",), "gap": ledger_rules.GAP_SECONDS})
        for broken in (None, {}, {"quiet": [], "gap_seconds": 0}, {"quiet": "甲", "gap_seconds": "60"},
                       {"quiet": ["甲", 3], "gap_seconds": True}):
            with self.subTest(attach=broken):
                self.assertEqual(settings(broken), built_in)

    def test_bad_arguments_are_rejected(self):
        state = LedgerState()
        for mark, kind in (("begin", None), ("end", "tower"), ("queue_end", "daily"), ("pause", None)):
            with self.assertRaises(ValueError):
                decide(mark, kind, state, task_id=1, rows=[], now=NOON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
