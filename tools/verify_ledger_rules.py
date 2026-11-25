"""库存记账运行时判定回归：流程走到一个位置时盘不盘、盘哪几类。

只测 agent/utils/ledger_rules.py 的纯函数：用假的任务起止与假的盘点结果按剧本走一遍，不需要 maa、不连游戏。直接运行：
    python -B tools/verify_ledger_rules.py -v
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import ledger_rules  # noqa: E402
from utils.ledger_rules import ALL_SCOPES, LedgerState, decide, game_day, settings, settle  # noqa: E402

QUIET = ("Env_MultiSave_Config", "StartGame_Start", "Env_Ledger_Config",
         "Collect_StartGame_HomePage", "Arbitrage_Start", "TokenShop_Start", "Close_HomePage", "Ledger_Settle_Manual")
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

    def test_rerun_right_after_a_closing_scan_resumes_it(self):
        """11-19 用户定：上一次运行刚在收尾位置盘完就重跑，第一个位置接着那次盘点；隔久了、没盘准、之后还有别的动静都照常全盘。"""
        def stamp(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

        def closing(unknown=None, termination="section_end", retry=None, extra=(), base=NOON):
            mark = {"kind": "ledger_mark", "mark": "end", "type": "shop", "session": "s1", "task": {"task_id": 7},
                    "node": "Ledger_End_Shop", "recorded_at": stamp(base)}
            origin = {key: mark[key] for key in ("mark", "type", "session", "task", "node")}
            scan = {"kind": "inventory_scan", "scan_id": "x1", "label": "记账·商店结束", "scope": ["A"],
                    "termination": termination, "unknown": unknown or {}, "origin": origin,
                    "finished_at": stamp(base + 70), "recorded_at": stamp(base + 70)}
            events = [{"kind": "ledger_mark", "mark": "begin", "recorded_at": stamp(base - 600)}, mark,
                      {"kind": "inventory_set", "recorded_at": stamp(base + 70)}, scan]
            if retry is not None:
                events.append({"kind": "inventory_scan", "scan_id": "x2", "origin": dict(origin, retry_of="x1"),
                               "recorded_at": stamp(base + 90), **retry})
            return events + [{"kind": "currency_reading", "recorded_at": stamp(base + 95)}, *extra]

        finished = frozenset({"section_end", "list_bottom", "all_found"})
        resume = ledger_rules.resume_from(closing(), now=NOON + 95 + 40, gap=60, finished=finished)
        self.assertEqual((resume["scan_id"], resume["scopes"], resume["at"]), ("x1", frozenset({"A"}), NOON + 70))
        filled = closing(unknown={"大蒜": "cell_unverified"}, retry={"quantities": {}, "absent": ["大蒜"], "unknown": {}})
        self.assertIsNotNone(ledger_rules.resume_from(filled, now=NOON + 130, gap=60, finished=finished))
        late = NOON + 12 * 3600 - 80    # 盘在游戏日最后一分多钟，重跑时已是下一个游戏日
        for name, events, now in (
                ("隔久了", closing(), NOON + 95 + 61),
                ("重扫没补上", closing(unknown={"大蒜": "x"}, retry={"absent": [], "unknown": {"大蒜": "x"}}), NOON + 130),
                ("没重扫", closing(unknown={"大蒜": "x"}), NOON + 130),
                ("盘点半路断了", closing(termination="stopped"), NOON + 130),
                ("收尾之后还有别的凭证", closing(extra=[{"kind": "inventory_invalidate", "recorded_at": stamp(NOON + 100)}]),
                 NOON + 130),
                ("最后是始标记", closing()[:1], NOON - 590),
                ("跨过游戏日", closing(base=late), late + 95 + 30)):
            with self.subTest(name):
                self.assertIsNone(ledger_rules.resume_from(events, now=now, gap=60, finished=finished))

        play = Play(start=NOON + 200)   # 接上之后：同类型不盘；换成日常只补它要的那几类
        play.task("Arbitrage_Start")
        play.state.last_scan = {"scan_id": "x1", "at": resume["at"], "scopes": resume["scopes"],
                                "game_day": resume["game_day"], "task_id": play.rows[-1]["task_id"]}
        self.assertFalse(play.at("begin", "shop")["scan"])
        self.assertEqual(play.at("end", "shop")["scopes"], ["A"])
        self.assertEqual(play.daily()["scopes"], ["B", "C"])

    def token_shop(self, play, bought=True):
        """代币商店一趟：进店前的 probe，买成过才在离店后留事后的始标记。返回 (probe 的判定, 始标记的判定或 None)。"""
        play.task("TokenShop_Start")
        probe = play.at("probe", "exchange")
        return probe, (play.at("begin", "exchange") if bought else None)

    def test_x1_token_shop_before_arbitrage_rides_its_closing_scan(self):
        """代币商店接入计划 §2：排在吸取与套利之间，进店前不盘、离店后不盘，套利收尾那一盘 A 与 B 一趟扫完。"""
        play = Play()
        play.daily()
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        play.at("end", "collect")
        probe, late = self.token_shop(play)
        shop_begin = play.task("Arbitrage_Start").at("begin", "shop")
        shop_end = play.at("end", "shop")
        close = play.task("Close_HomePage").at("queue_end")
        self.assertEqual(play.scans(), [FULL, FULL, ["A"], ["A", "B"]])
        self.assertFalse(probe["scan"] or late["scan"] or shop_begin["scan"] or close["scan"])
        self.assertTrue(shop_end["end_mark"])

    def test_x2_token_shop_that_bought_nothing_is_invisible(self):
        play = Play()
        play.daily()
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        play.at("end", "collect")
        self.token_shop(play, bought=False)
        play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        self.assertEqual(play.scans(), [FULL, FULL, ["A"], ["A"]])

    def test_x3_daily_straight_into_token_shop_scans_before_entering(self):
        """没排吸取：上一阶段（日常）还没收尾，进店前补盘，盘上日常与整个商店阶段要的；套利开头就不用再盘。"""
        play = Play()
        play.daily()
        probe, late = self.token_shop(play)
        shop_begin = play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        self.assertEqual(probe["scopes"], FULL)
        self.assertFalse(late["scan"] or shop_begin["scan"])
        self.assertEqual(play.scans(), [FULL, FULL, ["A", "B"]])
        nothing = Play()   # 一件没买成：进店前那一盘照样是日常的期末，套利开头一盘两用
        nothing.daily()
        self.token_shop(nothing, bought=False)
        self.assertFalse(nothing.task("Arbitrage_Start").at("begin", "shop")["scan"])
        self.assertEqual(nothing.at("end", "shop")["scopes"], ["A"])

    def test_x4_token_shop_first_in_run_scans_everything_before_entering(self):
        for gap in (3.0, 600.0):
            play = Play()
            if gap > 60:
                play.daily()
                play.task("Collect_StartGame_HomePage").at("begin", "collect")
                play.at("end", "collect")
            play.task("TokenShop_Start", gap=gap)
            with self.subTest(gap=gap):
                self.assertEqual(play.at("probe", "exchange")["scopes"], FULL)

    def test_x5_token_shop_after_arbitrage_is_closed_by_queue_end(self):
        play = Play()
        play.daily()
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        play.at("end", "collect")
        play.task("Arbitrage_Start").at("begin", "shop")
        play.at("end", "shop")
        probe, late = self.token_shop(play)
        close = play.task("Close_HomePage").at("queue_end")
        self.assertFalse(probe["scan"] or late["scan"])
        self.assertEqual(close["scopes"], ["B"])

    def test_x6_arbitrage_missing_its_scope_after_exchange_scans_both(self):
        """上一次盘点（手动结账收日常）没盘 A：套利开头连同兑换的 B 一起盘，不能只补 A 把 B 留在半路。"""
        play = Play()
        play.daily()
        settle_scan = play.task("Ledger_Settle_Manual").at("manual")
        self.token_shop(play)
        shop_begin = play.task("Arbitrage_Start").at("begin", "shop")
        shop_end = play.at("end", "shop")
        self.assertEqual(settle_scan["scopes"], ["B", "C"])
        self.assertEqual(shop_begin["scopes"], ["A", "B"])
        self.assertEqual(shop_end["scopes"], ["A"])

    def test_x7_late_begin_never_scans(self):
        """事后留的始标记处从不盘，哪怕是本次运行头一个位置：在这里盘会把兑换算进上一段。"""
        play = Play()
        play.task("TokenShop_Start")
        late = play.at("begin", "exchange")
        self.assertFalse(late["scan"])
        self.assertEqual([item["kind"] for item in play.state.pending], ["exchange"])
        self.assertEqual(play.task("Close_HomePage").at("queue_end")["scopes"], FULL)

    def test_x8_probe_leaves_no_activity_behind(self):
        play = Play()
        play.daily()
        play.task("Collect_StartGame_HomePage").at("begin", "collect")
        play.at("end", "collect")
        self.token_shop(play, bought=False)
        self.assertEqual(play.state.pending, [])
        self.assertFalse(play.task("Close_HomePage").at("queue_end")["scan"])

    def test_x9_probe_scan_after_a_close_blocks_resuming_it(self):
        """收尾之后进店前又补盘过：那一盘更新，重跑时不能拿收尾那一盘接（这一盘不留标记，只能看盘点记录）。"""
        def stamp(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

        mark = {"kind": "ledger_mark", "mark": "manual", "session": "s1", "task": {"task_id": 7}, "node": "Ledger_Manual",
                "recorded_at": stamp(NOON)}
        origin = {key: mark[key] for key in ("mark", "session", "task", "node")}
        scan = {"kind": "inventory_scan", "scan_id": "x1", "scope": ["B", "C"], "termination": "section_end", "unknown": {},
                "origin": origin, "finished_at": stamp(NOON + 70), "recorded_at": stamp(NOON + 70)}
        probe = {"kind": "inventory_scan", "scan_id": "x2", "scope": FULL, "termination": "section_end", "unknown": {},
                 "origin": {"mark": "probe", "type": "exchange", "session": "s2", "task": {"task_id": 1},
                            "node": "Ledger_Probe_Exchange"}, "finished_at": stamp(NOON + 90), "recorded_at": stamp(NOON + 90)}
        finished = frozenset({"section_end"})
        self.assertIsNotNone(ledger_rules.resume_from([mark, scan], now=NOON + 100, gap=60, finished=finished))
        self.assertIsNone(ledger_rules.resume_from([mark, scan, probe], now=NOON + 100, gap=60, finished=finished))

    def test_bad_arguments_are_rejected(self):
        state = LedgerState()
        for mark, kind in (("begin", None), ("end", "tower"), ("queue_end", "daily"), ("pause", None), ("probe", None)):
            with self.assertRaises(ValueError):
                decide(mark, kind, state, task_id=1, rows=[], now=NOON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
