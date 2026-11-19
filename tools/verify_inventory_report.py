"""库存记账的账与表回归：用假凭证按剧本走一遍，核对区间归属、归日、逐物品读数链、商店核对、估值与页面。

只测纯函数（agent/utils/inventory_ledger.py、inventory_report.py、inventory_report_render.py）与读写文件的那一层，
不需要 maa、不连游戏。直接运行：
    python -B tools/verify_inventory_report.py -v
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import inventory_report_output as output  # noqa: E402
from utils.inventory_ledger import build_ledger, dedupe  # noqa: E402
from utils.inventory_report import build_report  # noqa: E402
from utils.inventory_report_render import render_day_html, render_index_html  # noqa: E402
from utils.ledger_rules import QUIET  # noqa: E402

CATALOG = {"兽肉": {"category": "ingredient", "scope": "A", "kind": "食材"},
           "米": {"category": "ingredient", "scope": "A", "kind": "食材"},
           "包饭": {"category": "dish", "scope": "A", "kind": "料理"},
           "精炼石": {"category": "growth", "scope": "B", "kind": "精炼"},
           "抽抽乐券": {"category": "ticket", "scope": "B", "kind": "票券"},
           "素材": {"category": "craft", "scope": "C", "kind": "制作材料"}}
SCOPE = {name: row["scope"] for name, row in CATALOG.items()}
COOKBOOK = {"recipes": {"包饭": {"米": 3, "兽肉": 1}}, "purchasable": {"米"}}
NOON = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc).timestamp()   # 游戏日 11-14 的 12:00 KST
DAY = 86400
FULL = ("A", "B", "C")


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


class Story:
    """按剧本造凭证：task() 开一个任务，at() 到一个位置（给了范围就盘），其余方法改库存、记逐笔。"""

    def __init__(self, start=NOON):
        self.now, self.events, self.rows = start, [], []
        self.session, self.next_id, self.cursor, self.scans = "s1", 100, 0.0, 0
        self.stock = {"兽肉": 1000, "米": 500, "包饭": 10, "精炼石": 30, "抽抽乐券": 2, "素材": 5}
        self.money = {"gold": 1_000_000, "diamond": 500, "diamond_free": 500, "diamond_paid": 0, "tonic": 9000}
        self.unknown = ()
        self.lost = ()   # 下一次盘点里明明有、却被判成 0 的物品（漏读）
        self.cycles = {"Story_01_血骑士@g_weekly": "2026-11-07 12:00:00", "Daily_Mail@daily": "2026-11-13 12:00:00"}

    def restart(self, wait):
        """Agent 进程重启：新的会话号，任务起止从头记。"""
        self.end()
        self.now += wait
        self.session, self.rows, self.cursor = f"s{int(self.session[1:]) + 1}", [], 0.0
        return self

    def end(self):
        if self.rows and self.rows[-1]["end"] is None:
            self.rows[-1]["end"], self.rows[-1]["status"] = self.now, "succeeded"
        return self

    def task(self, entry, gap=3.0):
        self.end()
        self.now += gap
        self.next_id += 1
        self.rows.append({"entry": entry, "task_id": self.next_id, "start": self.now, "end": None, "status": None})
        self.now += 20
        return self

    def work(self, seconds=60, **changes):
        self.now += seconds
        for name, delta in changes.items():
            target = self.money if name in self.money else self.stock
            target[name] += delta
        return self

    def at(self, mark, kind=None, scopes=None, node=None, end_mark=True):
        row = self.rows[-1]
        node = node or f"Ledger_{mark}_{kind}"
        task = {"entry": row["entry"], "task_id": row["task_id"]}
        if mark != "end" or end_mark:
            self.events.append({"kind": "ledger_mark", "mark": mark, "type": kind, "at": iso(self.now),
                                **({"cards": dict(self.cycles)} if kind == "collect" else {}),
                                "observed_at": iso(self.now), "session": self.session, "task": task, "node": node,
                                "tasks": [{"entry": item["entry"], "task_id": item["task_id"], "start": iso(item["start"]),
                                           "end": None if item["end"] is None else iso(item["end"]),
                                           "status": item["status"]}
                                          for item in self.rows if item["end"] is None or item["end"] >= self.cursor]})
            self.cursor = self.now
        if scopes:
            self.scans += 1
            scan_id, started = f"scan{self.scans}", self.now + 5
            self.now += 100
            seen = {name: quantity for name, quantity in self.stock.items()
                    if SCOPE[name] in scopes and name not in self.unknown}
            self.events.append({
                "kind": "inventory_scan", "scan_id": scan_id, "seq": self.scans, "label": f"记账·{mark}",
                "started_at": iso(started), "finished_at": iso(self.now), "observed_at": iso(self.now),
                "scope": list(scopes), "complete": not any(SCOPE[name] in scopes for name in self.unknown),
                "termination": "list_bottom", "pages": 7,
                "quantities": {name: quantity for name, quantity in seen.items()
                               if quantity > 0 and name not in self.lost},
                "absent": [name for name, quantity in seen.items() if quantity == 0 or name in self.lost],
                "unknown": {name: "看不清" for name in self.unknown if SCOPE[name] in scopes},
                "origin": {"mark": mark, "type": kind, "node": node, "task": task, "session": self.session}})
            self.now += 8
            self.events.append({"kind": "currency_reading", "scan_id": scan_id, "observed_at": iso(self.now),
                                "label": f"记账·{mark}", **self.money})
            self.unknown = self.lost = ()
        return self

    def retry(self, *names):
        """存货没盘全，同一位置退出背包重扫：只找这几样，origin 带 retry_of 指回刚才那次盘点。"""
        parent = next(event for event in reversed(self.events)
                      if event["kind"] == "inventory_scan" and not event["origin"].get("retry_of"))
        self.scans += 1
        started = self.now + 2
        self.now += 40
        seen = {name: self.stock[name] for name in names}
        self.events.append({
            "kind": "inventory_scan", "scan_id": f"scan{self.scans}", "seq": self.scans, "label": f"{parent['label']}·重扫",
            "started_at": iso(started), "finished_at": iso(self.now), "observed_at": iso(self.now),
            "scope": ["A"], "complete": True, "termination": "list_bottom", "pages": 2,
            "quantities": {name: quantity for name, quantity in seen.items() if quantity > 0},
            "absent": [name for name, quantity in seen.items() if quantity == 0], "unknown": {},
            "origin": {**parent["origin"], "retry_of": parent["scan_id"]}})
        return self

    def sell(self, name, quantity, gold):
        self.now += 5
        self.stock[name] -= quantity
        self.money["gold"] += gold
        self.events.append({"kind": "inventory_set", "reason": "sale_confirmed", "observed_at": iso(self.now),
                            "quantities": {name: self.stock[name]},
                            "reference": {"actual_quantity": quantity, "inventory_after": self.stock[name],
                                          "proofs": [{"basis": "quoted_gold", "gold": {"before": 0, "after": gold,
                                                                                         "delta": gold}}]}})
        return self

    def buy(self, name, quantity, spent):
        self.now += 5
        self.stock[name] += quantity
        self.money["gold"] -= spent
        self.events.append({"kind": "inventory_set", "reason": "replenish_purchase", "observed_at": iso(self.now),
                            "quantities": {name: self.stock[name]},
                            "reference": {"actual_quantity": quantity, "actual_spent": spent}})
        return self

    def cook(self, dish, made, consistent=True):
        self.now += 5
        for name, amount in COOKBOOK["recipes"][dish].items():
            self.stock[name] -= amount * made
        self.stock[dish] += made
        self.events.append({"kind": "cooking_made", "recipe": dish, "made": made, "consistent": consistent,
                            "started_at": iso(self.now - 3), "observed_at": iso(self.now)})
        return self

    def loot(self, card, end="complete", seconds=30, **items):
        """一次进卡的吸取掉落凭证（utils/loot_cards.py 交出来的那种）：读到的条目，不动库存。"""
        self.now += seconds
        row = self.rows[-1]
        self.events.append({"kind": "collect_loot", "card": card, "end": end, "session": self.session,
                            "task": {"entry": row["entry"], "task_id": row["task_id"]},
                            "entered_at": iso(self.now - seconds), "first_at": iso(self.now - seconds + 5),
                            "last_at": iso(self.now), "at": iso(self.now), "observed_at": iso(self.now), "points": 3,
                            "items": dict(items), "unknown": {}, "unread": 0, "stuck": 0})
        return self

    def ledger(self):
        return build_ledger(self.events, catalog=CATALOG, quiet=QUIET)

    def report(self, day="2026-11-14", **options):
        return build_report(self.ledger(), game_day=day, as_of=iso(self.now), cookbook=COOKBOOK, **options)


def preset_run(story):
    """预设顺序的一轮：日常两个 → 吸取（正常结束）→ 商店 → 结束游戏。盘 4 次。"""
    story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=3, gold=5000)
    story.task("Mail_HomePage").at("begin", "daily").work(抽抽乐券=1)
    story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(600, 兽肉=400, 米=50)
    story.at("end", "collect", ("A",))
    story.task("Arbitrage_Start").at("begin", "shop")
    story.buy("米", 100, 1700).work(30, 米=40, gold=-700).cook("包饭", 20).sell("包饭", 25, 190_000)
    story.at("end", "shop", ("A",))
    story.task("Close_HomePage").at("queue_end")
    return story.end()


def next_morning(story):
    """次日第一次运行的第一盘。"""
    story.restart(DAY - 3000).task("Daily_HomePage").at("begin", "daily", FULL)
    return story


def item(model, name):
    return next(row for row in model["items"] if row["name"] == name)


class LedgerTests(unittest.TestCase):
    def test_preset_run_makes_three_typed_intervals(self):
        ledger = preset_run(Story()).ledger()
        self.assertEqual([scan["scope"] for scan in ledger["scans"]], [list(FULL), list(FULL), ["A"], ["A"]])
        self.assertEqual([(row["kind"], row["flags"]) for row in ledger["intervals"]],
                         [("daily", []), ("collect", []), ("shop", [])])
        self.assertEqual([task["entry"] for task in ledger["intervals"][0]["tasks"]], ["Daily_HomePage", "Mail_HomePage"])
        self.assertEqual([task["entry"] for task in ledger["intervals"][1]["tasks"]], ["Collect_StartGame_HomePage"])
        self.assertEqual(ledger["tail"]["types"], [])

    def test_item_chain_skips_scans_that_do_not_cover_it(self):
        ledger = next_morning(preset_run(Story())).ledger()
        pieces = ledger["items"]["精炼石"]["pieces"]
        self.assertEqual([(piece["from"], piece["to"], piece["delta"]) for piece in pieces], [(0, 1, 3), (1, 4, 0)])
        self.assertEqual(pieces[1]["reason"], "out_of_scope")
        self.assertEqual([piece["delta"] for piece in ledger["items"]["兽肉"]["pieces"]], [0, 400, -20, 0])

    def test_collect_interval_names_the_cards_finished_in_it(self):
        story = Story()
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 兽肉=100)
        story.cycles["Story_01_血骑士@g_weekly"] = "2026-11-14 12:10:00"      # 上周吸过，这一段又吸完一次
        story.cycles["Story_02_苍蓝魔女@g_weekly"] = "2026-11-14 12:20:00"    # 第一次吸完
        story.at("end", "collect", ("A",))
        interval, = story.end().ledger()["intervals"]
        self.assertEqual(interval["cards"], ["Story_01_血骑士", "Story_02_苍蓝魔女"])
        self.assertEqual(story.report()["collect"]["cards"], ["Story_01_血骑士", "Story_02_苍蓝魔女"])

    def test_collect_without_end_mark_is_abnormal_but_settled(self):
        story = Story()
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 兽肉=100)
        story.task("Arbitrage_Start").at("begin", "shop", ("A",)).work(60)
        story.at("end", "shop", ("A",))
        first, second = story.end().ledger()["intervals"]
        self.assertEqual((first["kind"], first["flags"]), ("collect", ["abnormal_end"]))
        self.assertIsNone(first["cards"])   # 没走到正常结束点，只有开头那一份快照，卡带不记名
        self.assertEqual((second["kind"], second["flags"]), ("shop", []))

    def test_scan_that_did_not_run_leaves_a_mixed_interval(self):
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=1)
        story.task("Collect_StartGame_HomePage").at("begin", "collect").work(300, 兽肉=100)   # 该补盘，没执行
        story.at("end", "collect", ("A",))
        interval, = story.end().ledger()["intervals"]
        self.assertEqual((interval["kind"], interval["types"]), ("mixed", ["daily", "collect"]))

    def test_unmarked_task_in_between_makes_the_interval_mixed(self):
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL)
        story.task("Fishing_Start").work(200, 兽肉=5)
        story.task("Close_HomePage").at("queue_end", None, FULL)
        interval, = story.end().ledger()["intervals"]
        self.assertEqual((interval["kind"], interval["strangers"]), ("mixed", ["Fishing_Start"]))

    def test_daily_closed_by_a_later_run_is_unsettled(self):
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=2)
        story.restart(3 * 3600).task("Daily_HomePage").at("begin", "daily", FULL)
        interval, = story.ledger()["intervals"]
        self.assertEqual((interval["kind"], interval["flags"]), ("daily", ["unsettled"]))

    def test_manual_settle_between_runs_is_outside(self):
        story = preset_run(Story())
        story.restart(3600).work(0, 兽肉=-30).task("Ledger_Settle_Manual").at("manual", None, FULL)
        self.assertEqual([row["kind"] for row in story.ledger()["intervals"]], ["daily", "collect", "shop", "outside"])

    def test_hot_and_cold_copies_are_merged(self):
        story = preset_run(Story())
        for number, event in enumerate(story.events):
            event["event_id"] = f"e{number}"
        doubled = story.events + [dict(event) for event in story.events[:9]]
        self.assertEqual(len(dedupe(doubled)), len(story.events))
        self.assertEqual(build_ledger(doubled, catalog=CATALOG, quiet=QUIET), story.ledger())
        bare = [{key: value for key, value in event.items() if key != "event_id"} for event in doubled]
        self.assertEqual(len(dedupe(bare)), len(story.events))


class ReportTests(unittest.TestCase):
    def test_interim_then_final(self):
        """U76：一日的账只算到当天最后一盘。次日第一盘到来时结账，结账用的就是最后一次记账的数；
        隔夜那一段不进上一日的表，差额记成次日的期初调整。"""
        story = preset_run(Story())
        interim = story.report()
        self.assertEqual((interim["status"], [col["key"] for col in interim["cols"]]),
                         ("interim", ["daily", "collect", "shop"]))
        self.assertEqual(item(interim, "精炼石")["closing_at"], "S2")   # 当天最后一次读到它的盘点
        self.assertIsNone(interim["balance"])
        story.work(0, 兽肉=-30, gold=-5000)                             # 结束游戏之后手动玩过
        final = next_morning(story).report()
        self.assertEqual(final["status"], "final")
        for key in ("cols", "segments", "items", "unchanged", "unread", "statement", "assets", "checks"):
            self.assertEqual(final[key], interim[key], key)             # 定稿不改数：就是最后一次记账的那张表
        self.assertEqual((item(final, "兽肉")["closing"], final["balance"]["items"]["兽肉"]["quantity"]), (1380, 1380))
        self.assertTrue(all(check["ok"] for check in final["checks"]), final["checks"])
        self.assertIsNone(story.report("2026-11-13"))
        today = story.report("2026-11-15")
        self.assertEqual(today["status"], "interim")
        self.assertEqual([(row["name"], row["before"], row["delta"], row["opening"], row["reason"]) for row in today["adjustments"]],
                         [("金币", 1_192_600, -5000, 1_187_600, "overnight"), ("兽肉", 1380, -30, 1350, "overnight")])
        self.assertEqual(today["overnight"]["tasks"], [])               # 那一段里没有记到任务：手动操作
        self.assertEqual({notice["level"] for notice in today["notices"] if "期初调整" in notice["text"]}, {"info"})

    def test_columns_hold_changes_not_readings(self):
        model = next_morning(preset_run(Story())).report()
        meat = item(model, "兽肉")
        self.assertEqual((meat["opening"], meat["deltas"], meat["closing"]),
                         (1000, {"daily": 0, "collect": 400, "shop": -20}, 1380))
        stone = item(model, "精炼石")
        # 吸取、商店那两段的两头盘点不都扫养成材料：这几格是「不扫」，合起来没变所以也没有跨区间的数
        self.assertEqual((stone["deltas"], stone["gaps"], stone["spans"]),
                         ({"daily": 3}, ["collect", "shop"], []))
        gold = item(model, "金币")
        self.assertEqual(gold["deltas"], {"daily": 5000, "collect": 0, "shop": 190_000 - 1700 - 700})
        self.assertEqual(model["statement"]["totals"]["cash"], gold["total"])

    def test_ticket_changed_while_unscanned_goes_to_a_span(self):
        story = preset_run(Story())
        story.stock["抽抽乐券"] += 0   # 日常里 +1 发生在第二盘之前，已在日常列
        story.work(0, 抽抽乐券=2)      # 结束游戏之后手动得了 2 张
        story.restart(1800).task("Daily_HomePage").at("begin", "daily", FULL)   # 同一天再跑一轮，第一盘全盘才读到
        ticket = item(story.report(), "抽抽乐券")
        self.assertEqual(ticket["deltas"], {"daily": 1})
        self.assertEqual([(span["cols"], span["delta"], span["reason"]) for span in ticket["spans"]],
                         [(["collect", "shop", "outside"], 2, "out_of_scope")])
        self.assertEqual(ticket["closing"], 5)

    def test_daily_rerun_after_the_last_scan_goes_to_the_next_opening(self):
        """U76：全套跑完又回去单跑一个日常，当天没有再盘：它的变化不在当天的表里，记成次日的期初调整，并写明那一段跑过什么。
        想让它进当天的日常列，当天得再盘一次（结束游戏、手动结账）。"""
        story = preset_run(Story())
        story.restart(1800).task("Pass_HomePage").at("begin", "daily", FULL).work(精炼石=7)
        model = next_morning(story).report()
        self.assertEqual([(col["key"], col["runs"]) for col in model["cols"]],
                         [("daily", 1), ("collect", 1), ("shop", 1), ("outside", 1)])
        self.assertEqual((item(model, "精炼石")["deltas"]["daily"], item(model, "精炼石")["closing"]), (3, 33))
        self.assertTrue(any("最后一次盘点之后还跑过日常" in notice["text"] for notice in model["notices"]))
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        today = story.report("2026-11-15", labels={"Pass_HomePage": "通行证"})
        (stone,) = today["adjustments"]
        self.assertEqual((stone["name"], stone["before"], stone["delta"], stone["reason"]), ("精炼石", 33, 7, "overnight"))
        self.assertEqual(today["overnight"]["tasks"], ["通行证"])
        self.assertIn("跑过：通行证，没有当场盘", render_day_html(today))
        settled = Story()                                                 # 换成当天收尾时盘了一次：进日常列
        preset_run(settled).restart(1800).task("Pass_HomePage").at("begin", "daily", FULL).work(精炼石=7)
        settled.task("Close_HomePage").at("queue_end", scopes=("B", "C"))
        self.assertEqual(item(settled.report(), "精炼石")["deltas"]["daily"], 10)

    def test_unknown_item_merges_its_two_intervals_only(self):
        story = Story()
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 兽肉=100, 米=10)
        story.unknown = ("米",)
        story.at("end", "collect", ("A",))
        story.task("Arbitrage_Start").at("begin", "shop").work(60, 米=-4, 兽肉=-1)
        story.at("end", "shop", ("A",))
        model = story.end().report()
        self.assertEqual(item(model, "兽肉")["deltas"], {"collect": 100, "shop": -1})
        rice = item(model, "米")
        self.assertEqual((rice["deltas"], rice["spans"][0]["cols"], rice["spans"][0]["delta"], rice["spans"][0]["reason"]),
                         ({}, ["collect", "shop"], 6, "unknown"))
        self.assertTrue(any("没读准" in notice["text"] for notice in model["notices"]))

    def test_zero_between_two_equal_readings_is_a_missed_read(self):
        """11-14 实机：吸取开头那次盘点把 9 万多个鸡蛋判成了 0，前后两次读数相同。"""
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=3)
        story.lost = ("兽肉",)
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 米=50)
        interim = story.report()   # 下一次盘点还没来：数照实列，但要提醒
        self.assertEqual(item(interim, "兽肉")["deltas"], {"daily": -1000})
        self.assertTrue(any("从 1,000 变成了 0" in notice["text"] for notice in interim["notices"]))
        story.at("end", "collect", ("A",))
        model = story.end().report()
        self.assertNotIn("兽肉", [row["name"] for row in model["items"]])      # 更正后没有变动
        self.assertIn("兽肉", [row["name"] for row in model["unchanged"]])
        self.assertTrue(any("S2 那次盘点把「兽肉」判成了 0" in notice["text"] for notice in model["notices"]))
        self.assertEqual(item(model, "米")["deltas"], {"daily": 0, "collect": 50})
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

    def test_missed_read_is_noticed_only_on_its_own_day(self):
        """11-18 实机：11-14 那次把鸡蛋判成 0 的提醒，跟着鸡蛋进了 11-15、11-18 的报表。只报在漏读那天。"""
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=3)
        story.lost = ("兽肉",)
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 米=50)
        story.at("end", "collect", ("A",))
        next_morning(story)
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 兽肉=40)
        story.at("end", "collect", ("A",))
        first = story.end().report()
        self.assertTrue(any("S2 那次盘点把「兽肉」判成了 0" in notice["text"] for notice in first["notices"]))
        model = story.report(day="2026-11-15")
        meat = item(model, "兽肉")
        self.assertEqual((meat["deltas"].get("collect"), meat["suspect"]), (40, []))
        self.assertFalse(any("判成了 0" in notice["text"] for notice in model["notices"]), model["notices"])
        self.assertEqual(model["kpis"]["warnings"], 0)
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

    def test_small_or_sold_out_items_may_really_hit_zero(self):
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(抽抽乐券=-2)   # 两张券用光
        story.task("Arbitrage_Start").at("begin", "shop", FULL).work(10, 抽抽乐券=2).sell("包饭", 10, 70_000)
        story.at("end", "shop", ("A",))
        story.work(10, 包饭=10)
        story.task("Close_HomePage").at("queue_end", None, FULL)
        ledger = story.end().ledger()
        self.assertEqual(ledger["items"]["抽抽乐券"]["suspect"], [])          # 数量小：有 → 0 → 同一个数是可能的
        self.assertEqual([piece["delta"] for piece in ledger["items"]["抽抽乐券"]["pieces"]], [-2, 2])
        self.assertEqual(ledger["items"]["包饭"]["suspect"], [])              # 有出售记录：卖光是真的

    def test_shop_reconciliation_leaves_regular_purchase_as_residual(self):
        model = preset_run(Story()).report()
        rows = {row["name"]: row for row in model["shop"]["rows"]}
        self.assertEqual((rows["米"]["net"], rows["米"]["replenish"], rows["米"]["cooking"], rows["米"]["residual"]),
                         (80, 100, -60, 40))
        self.assertEqual((rows["兽肉"]["cooking"], rows["兽肉"]["residual"]), (-20, 0))
        self.assertEqual((rows["包饭"]["net"], rows["包饭"]["sales"], rows["包饭"]["cooking"], rows["包饭"]["residual"]),
                         (-5, -25, 20, 0))
        self.assertEqual(model["shop"]["gold"], {"net": 187_600, "sales": 190_000, "replenish": -1700, "residual": -700,
                                                 "unconfirmed_sales": 0, "carried": 0, "unknown_sales": [],
                                                 "regular": True})
        self.assertEqual(model["shop"]["mismatch"], [])

    def test_sale_confirmed_by_stock_takes_the_quote_or_stays_unknown(self):
        """11-16 试跑7：金币没核上、回读库存确认卖光——按开窗时核对过的报价记；少的不是选量那么多、或没有报价，记未知（用户 11-16）。"""
        def run(proof):
            story = Story()
            story.task("Arbitrage_Start").at("begin", "shop", FULL)
            story.sell("包饭", 10, 70_000)
            story.events[-1]["reference"]["proofs"] = [proof]
            story.at("end", "shop", ("A",))
            return story.end().report()

        model = run({"basis": "inventory_delta", "before": 40, "after": 30, "quantity": 10, "selected": 10,
                     "quoted_total": 70_000})
        money = model["shop"]["gold"]
        self.assertEqual((money["sales"], money["carried"], money["residual"], money["regular"]), (70_000, 70_000, 0, True))
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        for label, proof in (("只卖成一部分", {"basis": "inventory_delta", "before": 40, "after": 35, "quantity": 5,
                                              "selected": 10, "quoted_total": 70_000}),
                             ("旧凭证没有报价", {"basis": "inventory_delta", "before": 40, "after": 30, "quantity": 10})):
            with self.subTest(label):
                model = run(proof)
                money = model["shop"]["gold"]
                self.assertEqual((money["sales"], money["residual"], money["unknown_sales"], money["regular"]),
                                 (0, 70_000, ["包饭"], False))
                check = next(check for check in model["checks"] if check["name"].startswith("C3"))
                self.assertFalse(check["ok"])
                self.assertIn("包饭 的出售没核到金币", check["detail"])
                self.assertIn("余额项 +70,000 为正，不是常规采购", check["detail"])
                labels = [row["label"] for row in model["statement"]["rows"]]
                self.assertIn("商店套利：未分清的金币（余额项）", labels)
                self.assertNotIn("商店套利：常规采购支出", labels)

    def test_shop_mismatch_is_reported_not_smoothed(self):
        story = Story()
        story.task("Arbitrage_Start").at("begin", "shop", FULL).cook("包饭", 20).work(10, 兽肉=-7, 包饭=3)
        story.at("end", "shop", ("A",))
        model = story.end().report()
        self.assertEqual(sorted(model["shop"]["mismatch"]), ["兽肉", "包饭"])
        self.assertTrue(any("推算失配" in notice["text"] for notice in model["notices"]))

    def test_valuation_splits_quantity_and_price(self):
        story = next_morning(preset_run(Story()))
        ledger = story.ledger()
        prices = {scan["scan_id"]: {"兽肉": {"unit": 8}, "米": {"unit": 10}, "包饭": {"unit": 7800}}
                  for scan in ledger["scans"]}
        prices["scan4"]["包饭"] = prices["scan5"]["包饭"] = {"unit": 7868}   # 商店结束那次起换成实测价
        model = build_report(ledger, game_day="2026-11-14", as_of=iso(story.now), prices=prices, cookbook=COOKBOOK)
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        dish = model["valuation"]["dish"]
        self.assertEqual((dish["opening"], dish["closing"], dish["reval"], dish["flows"]["shop"]),
                         (10 * 7800, 5 * 7868, 10 * 68, -5 * 7868))
        self.assertEqual(model["valuation"]["ingredient"]["flows"]["collect"], 400 * 8 + 50 * 10)
        totals = model["statement"]["totals"]
        self.assertEqual(totals["profit"], totals["cash"] + totals["stock"])
        self.assertEqual(model["statement"]["closing"]["total"], model["statement"]["opening"]["total"] + totals["profit"])
        del prices["scan1"]["米"]
        model = build_report(ledger, game_day="2026-11-14", as_of=iso(story.now), prices=prices, cookbook=COOKBOOK)
        self.assertEqual(model["valuation"]["ingredient"]["unpriced"], ["米"])
        ledger["items"]["土豆"] = {"category": "ingredient", "kind": "食材", "scope": "A", "suspect": [],
                                   "readings": [[0, 0], [4, 0]],
                                   "pieces": [{"from": 0, "to": 4, "open": 0, "close": 0, "delta": 0}]}
        model = build_report(ledger, game_day="2026-11-14", as_of=iso(story.now), prices=prices, cookbook=COOKBOOK)
        self.assertEqual(model["valuation"]["ingredient"]["unpriced"], ["米"])   # 一直是 0 的没有单价也不提醒
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

    def test_closing_scan_that_missed_something_adjusts_the_next_opening(self):
        """U67-1：结账那一盘没读准兽肉、没读到顶栏，次日补上读数时记成期初调整；上一日不动，不进本日各列与损益。"""
        story = preset_run(Story()).work(0, 兽肉=-30, gold=-5000)        # 结束游戏之后手动玩
        story.unknown = ("兽肉",)
        next_morning(story)
        story.events.pop()                                              # 结账那一盘的货币读数没读到
        story.task("Mail_HomePage").work(60, 兽肉=10, gold=1000).at("begin", "daily", FULL)
        ledger = story.ledger()
        prices = {scan["scan_id"]: {"兽肉": {"unit": 8}, "米": {"unit": 10}, "包饭": {"unit": 7800}}
                  for scan in ledger["scans"]}
        prices["scan6"]["兽肉"] = {"unit": 9}
        before = build_report(ledger, game_day="2026-11-14", as_of=iso(story.now), prices=prices, cookbook=COOKBOOK)
        meat, gold = item(before, "兽肉"), item(before, "金币")
        self.assertEqual((meat["closing"], meat["closing_at"], meat["spans"], meat["adjust"]), (1380, "S4", [], None))
        self.assertEqual((gold["closing"], gold["closing_at"], gold["adjust"]), (1_192_600, "S4", None))
        self.assertTrue(all(check["ok"] for check in before["checks"]), before["checks"])
        model = build_report(ledger, game_day="2026-11-15", as_of=iso(story.now), prices=prices, cookbook=COOKBOOK)
        self.assertEqual([row["name"] for row in model["items"]], [])                 # 当天本身没有变动：进销存表里不列
        gold, meat = model["adjustments"]                                             # 期初调整表只列对不上的，货币在前
        self.assertEqual({key: meat[key] for key in ("name", "before", "before_at", "delta", "opening", "opening_at", "reason",
                                                     "value")},
                         {"name": "兽肉", "before": 1380, "before_at": "S4", "delta": -20, "opening": 1360, "opening_at": "S6",
                          "reason": "unknown", "value": 1360 * 9 - 1380 * 8})
        self.assertEqual((gold["name"], gold["before"], gold["delta"], gold["opening"], gold["reason"]),
                         ("金币", 1_192_600, -4000, 1_188_600, "unread"))
        statement = model["statement"]
        self.assertEqual(statement["adjustment"], {"cash": -4000, "stock": 1360 * 9 - 1380 * 8,
                                                   "total": -4000 + 1360 * 9 - 1380 * 8})
        self.assertEqual(statement["carried"]["total"], before["statement"]["closing"]["total"])   # 上日期末 = 上一日表上的期末
        self.assertEqual(statement["totals"]["cash"], 0)                                          # 调整不进本日合计
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        self.assertEqual(model["kpis"]["adjusted"], 2)
        self.assertIn("期初调整 2 项", model["summary"])
        self.assertTrue(any("见「期初调整」表" in notice["text"] for notice in model["notices"]))
        page = render_day_html(model)
        table = page[page.index("<h2>期初调整"):page.index("<h2>一、进销存表")]
        self.assertIn("<td class=\"sticky\">兽肉</td>", table)
        self.assertIn("本日第一盘没读准它", table)
        self.assertIn("本日第一盘没读到顶栏", table)
        self.assertNotIn("调整 ", page[page.index("<h2>一、进销存表"):page.index("<h2>三、资产表")])   # 进销存表不再标注
        self.assertNotIn("<h2>期初调整", render_day_html(before))                                  # 平时没有这张表

    def test_settled_table_carries_a_closing_balance_to_the_next_day(self):
        """U75 结转：定稿的表带期末余额，每样东西一行——各自当天最后一次读到的数，当天没读到的沿用上一张表的那一行；
        下一日的上日期末取自它。"""
        story = preset_run(Story())
        story.unknown = ("抽抽乐券",)
        next_morning(story)                                              # S5：11-15 第一盘，没读准抽抽乐券
        story.task("Arbitrage_Start").at("begin", "shop").work(30, 兽肉=5)
        story.at("end", "shop", ("A",))                                  # S6：11-15 只再盘了一次存货
        story.work(0, 抽抽乐券=2)
        story.unknown = ("素材",)
        next_morning(story)                                              # S7：11-16 第一盘，没读准素材；抽抽乐券这才读到
        ledger = story.ledger()
        at = [scan["at"] for scan in ledger["scans"]]

        def report(day, prior=None):
            return build_report(ledger, game_day=day, as_of=iso(story.now), cookbook=COOKBOOK, prior=prior)

        first = report("2026-11-14")
        self.assertEqual(first["balance"]["as_of"], at[3])                                        # 当天最后一盘
        self.assertLessEqual(set(CATALOG) | {"金币"}, set(first["balance"]["items"]))
        self.assertEqual(first["balance"]["items"]["兽肉"], {"quantity": 1380, "at": at[3]})
        self.assertEqual(first["balance"]["items"]["抽抽乐券"], {"quantity": 3, "at": at[1]})     # 最后两盘不扫它：当天更早的读数
        second = report("2026-11-15", first["balance"]["items"])
        self.assertEqual(second["unread"], ["抽抽乐券"])
        self.assertEqual(second["adjustments"], [])
        self.assertEqual(second["balance"]["items"]["抽抽乐券"], {"quantity": 3, "at": at[1]})    # 当天没读到：沿用上一张表
        self.assertEqual(second["balance"]["items"]["素材"], {"quantity": 5, "at": at[4]})
        self.assertEqual(second["balance"]["items"]["兽肉"], {"quantity": 1385, "at": at[5]})
        third = report("2026-11-16", second["balance"]["items"])
        self.assertIsNone(third["balance"])                                                      # 期中的表没有期末余额
        (ticket,) = third["adjustments"]
        self.assertEqual({key: ticket[key] for key in ("name", "before", "before_at", "before_time", "delta", "opening",
                                                       "reason")},
                         {"name": "抽抽乐券", "before": 3, "before_at": "S2", "before_time": at[1], "delta": 2, "opening": 5,
                          "reason": "overnight"})
        # 手上没有定稿的表（离线拿整本账单算某一天）时从账里推上日期末，与逐日结转的结果相同
        self.assertEqual(report("2026-11-15")["balance"], second["balance"])
        self.assertEqual(report("2026-11-16")["adjustments"], third["adjustments"])
        page = render_day_html(second)
        block = page[page.index("<summary>期末余额"):]
        block = block[:block.index("</details>")]
        self.assertIn("1 项当天没读到、沿用更早的读数", block)
        self.assertIn('<td class="sticky">抽抽乐券</td>', block)
        self.assertIn("（沿用）", block)
        self.assertNotIn('<td class="sticky">兽肉</td>', block)                                    # 当天读到过的不重复列
        self.assertNotIn("<summary>期末余额", render_day_html(third))

    def test_first_day_and_tail_are_announced(self):
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=1)
        model = story.report()
        self.assertTrue(model["first_scan"])
        self.assertEqual((model["segments"], [row["name"] for row in model["items"]]), ([], []))
        self.assertTrue(any("没盘掉的活动（日常）" in notice["text"] for notice in model["notices"]))


def collect_run(story, *cards, finished=None, **changes):
    """一次吸取任务：开头全盘 → 各次进卡（卡名, end, 读到的条目）→ 正常结束只盘 A。finished 是这一段吸完的卡。"""
    story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, **changes)
    for card, end, items in cards:
        story.loot(card, end, **items)
    for minute, card in enumerate(finished if finished is not None else [c for c, end, _ in cards if end == "complete"]):
        story.cycles[f"{card}@g_weekly"] = f"2026-11-14 12:{10 + minute:02d}:00"
    return story.at("end", "collect", ("A",))


def split(model):
    """分卡产出 → {卡带: {物品: 数量}}，卡带未知记在 None 下。"""
    by_card = model["collect"]["by_card"]
    result = {row["card"]: {item["name"]: item["quantity"] for item in row["items"]} for row in by_card["rows"]}
    result[None] = {item["name"]: item["quantity"] for item in by_card["unallocated"]["items"]}
    return result


class CardSplitTests(unittest.TestCase):
    A, B, C = "Story_01_血骑士", "Story_02_苍蓝魔女", "Story_03_迷雾神射手"

    def test_totals_are_split_by_what_each_card_read(self):
        # 吸完的卡都有记录也不放大：盘点比读到的多出来的说不清是哪张卡的（走路碰到的），归「卡带未知」（U60）
        story = collect_run(Story(), (self.A, "complete", {"兽肉": 60, "金币": 2000}),
                            (self.B, "complete", {"兽肉": 40, "金币": 2000}), 兽肉=100, gold=5000)
        model = story.end().report()
        self.assertEqual(split(model), {self.A: {"兽肉": 60, "金币": 2000}, self.B: {"兽肉": 40, "金币": 2000},
                                        None: {"金币": 1000}})
        by_card = model["collect"]["by_card"]
        self.assertEqual((by_card["runs"], by_card["recorded"], by_card["covered"]), (1, 1, 1))
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        self.assertIn("C8 各卡读到 + 卡带未知 = 吸取列（逐物品）", [check["name"] for check in model["checks"]])

    def test_unrecorded_finished_card_keeps_reads_and_leaves_the_rest(self):
        # C 吸完了却没有掉落记录：不放大，照读到的数列，差额归「卡带未知」，C 灰着列出来
        story = collect_run(Story(), (self.A, "complete", {"兽肉": 60, "金币": 2000}),
                            (self.B, "complete", {"兽肉": 40, "金币": 2000}),
                            finished=[self.A, self.B, self.C], 兽肉=400, gold=5000)
        model = story.end().report()
        self.assertEqual(split(model), {self.A: {"兽肉": 60, "金币": 2000}, self.B: {"兽肉": 40, "金币": 2000},
                                        self.C: {}, None: {"兽肉": 300, "金币": 1000}})
        silent = next(row for row in model["collect"]["by_card"]["rows"] if row["card"] == self.C)
        self.assertEqual((silent["recorded"], silent["finished"]), (False, True))
        self.assertEqual(model["collect"]["by_card"]["covered"], 0)
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

    def test_reading_more_than_counted_is_not_scaled(self):
        # 读到的比盘点多也不缩小：各卡照读数列，差额在「卡带未知」里记负数（U60）
        story = collect_run(Story(), (self.A, "complete", {"兽肉": 300}), (self.B, "complete", {"兽肉": 200}),
                            finished=[self.A, self.B, self.C], 兽肉=400)
        model = story.end().report()
        self.assertEqual(split(model), {self.A: {"兽肉": 300}, self.B: {"兽肉": 200}, self.C: {}, None: {"兽肉": -100}})
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        self.assertIn('卡带未知</td><td></td><td class="note">兽肉 <span class="neg">(100)</span>', render_day_html(model))

    def test_same_card_twice_in_a_day_is_one_row(self):
        story = collect_run(Story(), (self.A, "task_end", {"兽肉": 50}), 兽肉=100)   # 次数用完，卡没吸完
        story.task("Close_HomePage").at("queue_end")
        collect_run(story.end().restart(3600), (self.A, "complete", {"兽肉": 30}), 兽肉=60)
        model = story.end().report()
        row, = [row for row in model["collect"]["by_card"]["rows"] if row["card"] == self.A]
        self.assertEqual((row["visits"], row["complete"], row["items"]), (2, 1, [{"name": "兽肉", "quantity": 80,
                                                                                   "value": None}]))
        self.assertEqual(split(model)[None], {"兽肉": 80})
        self.assertEqual(model["collect"]["by_card"]["runs"], 2)
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

    def test_items_the_scan_skips_are_only_listed_as_reads(self):
        story = collect_run(Story(), (self.A, "complete", {"兽肉": 10, "精炼石": 4}), 兽肉=20, 精炼石=4)
        row = next(row for row in story.end().report()["collect"]["by_card"]["rows"] if row["card"] == self.A)
        self.assertEqual(row["items"], [{"name": "兽肉", "quantity": 10, "value": None}])
        self.assertEqual(row["read_only"], [{"name": "精炼石", "quantity": 4}])

    def test_unknown_card_and_values(self):
        story = collect_run(Story(), (None, "later", {"兽肉": 10}), (self.A, "complete", {"兽肉": 30}),
                            finished=[self.A], 兽肉=80, gold=1000)
        ledger = story.end().ledger()
        prices = {scan["scan_id"]: {"兽肉": {"unit": 8}} for scan in ledger["scans"]}
        model = build_report(ledger, game_day="2026-11-14", as_of=iso(story.now), prices=prices, cookbook=COOKBOOK)
        rows = {row["card"]: row for row in model["collect"]["by_card"]["rows"]}
        self.assertEqual((rows[self.A]["items"][0]["quantity"], rows[self.A]["value"]), (30, 30 * 8))
        self.assertEqual(rows[None]["items"][0]["quantity"], 10)   # 卡名读不到的那次进卡照样分，列作「未识别卡带」
        self.assertEqual(model["collect"]["by_card"]["unallocated"]["items"],
                         [{"name": "金币", "quantity": 1000, "value": 1000}, {"name": "兽肉", "quantity": 40, "value": 40 * 8}])
        self.assertIn("未识别卡带", render_day_html(model))

    def test_stock_rescan_merges_into_the_same_scan(self):
        # 吸取结束那次盘点兽肉没读准，同一位置退出重扫补上：账上还是一次盘点，兽肉照常算吸取、不跨区间（U61）
        story = Story()
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 兽肉=400, 米=50)
        story.unknown = ("兽肉",)
        story.at("end", "collect", ("A",)).retry("兽肉")
        ledger = story.end().ledger()
        self.assertEqual(len(ledger["scans"]), 2)
        closing = ledger["scans"][-1]
        self.assertEqual((closing["retried"], closing["unknown"], closing["complete"], closing["readings"]["兽肉"]),
                         (1, [], True, 1400))
        model = story.report()
        beef = next(item for item in model["items"] if item["name"] == "兽肉")
        self.assertEqual((beef["deltas"].get("collect"), beef["spans"]), (400, []))
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])
        self.assertIn("存货退出重扫过", render_day_html(model))
        # 重扫指回的那次盘点找不到：照一次普通盘点记，不丢读数
        story.events = [event for event in story.events if event.get("scan_id") != "scan2"]
        self.assertEqual([scan["scan_id"] for scan in story.ledger()["scans"]], ["scan1", "scan3"])

    def test_no_loot_records_keeps_the_old_section(self):
        story = collect_run(Story(), finished=[self.A], 兽肉=100)
        model = story.end().report()
        self.assertEqual(model["collect"]["by_card"]["recorded"], 0)
        self.assertNotIn("C8", " ".join(check["name"] for check in model["checks"]))
        page = render_day_html(model)
        self.assertIn("不拆分卡产出", page)
        self.assertNotIn("分卡产出 <small>", page)

    def test_page_lists_cards_and_duplicates_are_dropped(self):
        story = collect_run(Story(), (self.A, "complete", {"兽肉": 60}), (self.B, "complete", {"兽肉": 40}),
                            finished=[self.A, self.B, self.C], 兽肉=400)
        story.events += [dict(event) for event in story.events if event["kind"] == "collect_loot"]   # 热档冷档各一份
        model = story.end().report()
        self.assertEqual(len(model["segments"][0]["loot"]), 2)
        page = render_day_html(model)
        for needle in ("五、吸取 · 分卡产出", "剧情 01 血骑士", "剧情 03 迷雾神射手", "未记录掉落", "卡带未知", "（十）吸取分卡产出"):
            self.assertIn(needle, page)
        self.assertNotIn("None", page)


class RenderTests(unittest.TestCase):
    def page(self, **options):
        story = next_morning(preset_run(Story()))
        model = story.report(account={"hash": "3f9a0c21", "alias": "A 号"}, **options)
        return model, render_day_html(model)

    def test_page_is_one_self_contained_file(self):
        model, page = self.page()
        for needle in ("http://", "https://", 'src="//', "<script src", "<link"):
            self.assertNotIn(needle, page)
        self.assertEqual(page.count("<script"), 1)
        for needle in ("库存日报", "期末", "一、进销存表", "二、商店账实核对", "三、资产表", "四、收益与现金流量表",
                       "不扫", "兽肉", "A 号", model["summary"]):
            self.assertIn(needle, page)
        self.assertNotIn("None", page)

    def test_interim_page_says_so(self):
        model = preset_run(Story()).report()
        page = render_day_html(model)
        self.assertIn("期中", page)
        self.assertIn("本日尚未结账", page)

    def test_failed_check_is_shown_and_page_still_renders(self):
        model, _ = self.page()
        model["checks"][0].update(ok=False, detail="不平：兽肉")
        model["notices"].insert(0, {"level": "error", "text": "自检失败：C1"})
        page = render_day_html(model)
        self.assertIn("自检失败", page)
        self.assertIn('class="bad"', page)

    def test_index_lists_days(self):
        model, _ = self.page()
        page = render_index_html([model])
        self.assertIn("2026-11-14", page)
        self.assertIn("2026-11-14.html", page)
        self.assertNotIn("None", page)


class OutputTests(unittest.TestCase):
    def test_catalog_matches_the_scan_scopes(self):
        catalog = output.load_catalog(ROOT / "agent")
        scopes = json.loads((ROOT / "assets/resource/base/pipeline/BagScan.json").read_text(encoding="utf-8"))[
            "Agt_BagScan_Config"]["attach"]["scopes"]
        expected = {profile: key for key, row in scopes.items() for profile in row["profiles"]}
        self.assertEqual(output.PROFILE_SCOPE, expected)
        self.assertEqual((catalog["兽肉"]["scope"], catalog["兽肉"]["category"]), ("A", "ingredient"))
        self.assertEqual(catalog["牛奶"]["category"], "ingredient")   # 亲密饮品里有同名的，不能盖掉食材
        self.assertTrue(any(row["category"] == "ticket" for row in catalog.values()))

    def test_vouchers_come_from_hot_and_cold_and_reports_are_written(self):
        story = next_morning(preset_run(Story()))
        for number, event in enumerate(story.events):
            event["event_id"] = f"e{number}"
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            cold = output.history_dir(store, "42")
            cold.mkdir(parents=True)
            lines = [json.dumps(event, ensure_ascii=False) for event in story.events[:20]]
            (cold / "2026-11-14.events.jsonl").write_text("\n".join(lines + ["{坏行"]) + "\n", encoding="utf-8")
            (store / "agent_save_data_42.json").write_text(json.dumps(
                {"arbitrage": {"inventory": {"events": story.events[12:]}}}, ensure_ascii=False), encoding="utf-8")
            vouchers = output.load_vouchers(store, "42")
            self.assertEqual(len(dedupe(vouchers)), len(story.events))
            result = output.generate_reports(store, "42", agent_dir=ROOT / "agent", as_of=iso(story.now))
            folder_out = output.report_dir(store, "42")
            self.assertEqual(sorted(result["days"]), ["2026-11-14", "2026-11-15"])
            self.assertEqual(json.loads((folder_out / "2026-11-14.json").read_text(encoding="utf-8"))["status"], "final")
            self.assertEqual(json.loads((folder_out / "2026-11-15.json").read_text(encoding="utf-8"))["status"], "interim")
            for name in ("2026-11-14.html", "2026-11-15.html", "index.html", "latest.html"):
                self.assertTrue((folder_out / name).stat().st_size > 1000, name)
            self.assertEqual((folder_out / "latest.html").read_text(encoding="utf-8"),
                             (folder_out / "2026-11-15.html").read_text(encoding="utf-8"))
            # 已定稿的日子不再重写；只重算还没结账的
            stamp = (folder_out / "2026-11-14.json").stat().st_mtime_ns
            again = output.generate_reports(store, "42", agent_dir=ROOT / "agent", as_of=iso(story.now + 60))
            self.assertEqual(again["days"], ["2026-11-15"])
            self.assertEqual((folder_out / "2026-11-14.json").stat().st_mtime_ns, stamp)
            # 更早的日子快照被删掉了也不凭残缺的凭证重做：那天的冷档这一回没读
            (cold / "2026-11-13.events.jsonl").write_text(json.dumps(
                {**story.events[1], "event_id": "old", "scan_id": "old", "finished_at": "2026-11-13T05:00:00+00:00",
                 "started_at": "2026-11-13T04:58:00+00:00"}, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(output.generate_reports(store, "42", agent_dir=ROOT / "agent")["days"], ["2026-11-15"])
            everything = output.generate_reports(store, "42", agent_dir=ROOT / "agent", force=True)["days"]
            self.assertEqual(everything, ["2026-11-13", "2026-11-14", "2026-11-15"])

    def test_late_reading_after_settlement_matches_a_forced_rebuild(self):
        """穿透复核第 1 条：结账那一盘没读到的东西后来补上，运行时随盘随出与强制重算的结果相同，那笔差不会丢。"""
        story = preset_run(Story())
        story.unknown = ("精炼石",)
        next_morning(story)
        story.work(0, 精炼石=-12)
        story.task("Mail_HomePage").at("begin", "daily", FULL)
        for number, event in enumerate(story.events):
            event["event_id"] = f"e{number}"
        cut = max(index for index, event in enumerate(story.events) if event.get("scan_id") == "scan5") + 1

        def rows(folder):
            out = {}
            for day in ("2026-11-14", "2026-11-15"):
                model = json.loads((folder / f"{day}.json").read_text(encoding="utf-8"))
                listed = model["items"] + [{"name": row["name"], "opening": row["quantity"], "closing": row["quantity"]}
                                           for row in model["unchanged"]]
                stone = next(row for row in listed if row["name"] == "精炼石")
                adjust = next((row for row in model["adjustments"] if row["name"] == "精炼石"), None)
                out[day] = (stone["opening"], stone["closing"], adjust)
            return out

        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=story.events[:cut])   # 次日第一盘：上一日定稿
            output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=story.events)          # 补上读数
            running = rows(output.report_dir(store, "42"))
            output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=story.events, force=True)
            self.assertEqual(rows(output.report_dir(store, "42")), running)
        self.assertEqual(running["2026-11-14"], (30, 33, None))
        self.assertEqual(running["2026-11-15"][:2], (21, 21))
        self.assertEqual({key: running["2026-11-15"][2][key] for key in ("before", "delta")}, {"before": 33, "delta": -12})

    def test_adjustment_stays_once_its_day_is_settled(self):
        """U75：某样东西在最后一个定稿日整天没读到、上次的读数又滚出了热档，期初调整在补上读数那天定稿之后也还在。

        上日期末取自上一张定稿的表，不靠更早的凭证；那张是没有余额块的旧表时，这一回把冷档读全从账里推。
        """
        story = preset_run(Story())
        story.unknown = ("精炼石",)
        next_morning(story)                    # 11-15 唯一一次全盘没读准精炼石
        story.work(0, 精炼石=-12)
        next_morning(story)                    # 11-16 第一盘读到 21
        next_morning(story)                    # 11-17 第一盘：11-16 结账
        by_day = {}
        for number, event in enumerate(story.events):
            event["event_id"] = f"e{number}"
            by_day.setdefault(event["observed_at"][:10], []).append(event)
        self.assertEqual(sorted(by_day), ["2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17"])

        def stone(folder):
            model = json.loads((folder / "2026-11-16.json").read_text(encoding="utf-8"))
            return model["status"], next(({key: row[key] for key in ("before", "delta", "opening")}
                                          for row in model["adjustments"] if row["name"] == "精炼石"), None)

        for old_table in (False, True):
            with self.subTest(old_table=old_table), tempfile.TemporaryDirectory() as folder:
                store = Path(folder)
                cold, reports = output.history_dir(store, "42"), output.report_dir(store, "42")
                cold.mkdir(parents=True)
                for day, events in sorted(by_day.items()):   # 每天一回：当天的凭证进冷档，热档只有当天这几条
                    (cold / f"{day}.events.jsonl").write_text(
                        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")
                    if day == "2026-11-16":                  # 定稿的表格式版本不同也不重算
                        path = reports / "2026-11-14.json"
                        path.write_text(path.read_text(encoding="utf-8").replace('"ledger-report/2"', '"ledger-report/0"'),
                                        encoding="utf-8")
                    if old_table and day == "2026-11-17":    # 最后一张定稿的表是没有期末余额的旧表
                        path = reports / "2026-11-15.json"
                        model = json.loads(path.read_text(encoding="utf-8"))
                        del model["balance"]
                        path.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
                    output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=events)
                self.assertEqual(stone(reports), ("final", {"before": 33, "delta": -12, "opening": 21}))
                self.assertIn('"ledger-report/0"', (reports / "2026-11-14.json").read_text(encoding="utf-8"))
                output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=[], force=True)
                self.assertEqual(stone(reports), ("final", {"before": 33, "delta": -12, "opening": 21}))

    def test_zero_frozen_at_settlement_is_put_right_by_an_adjustment(self):
        """U76：当天最后一盘把一样东西漏读成 0，这一天就以 0 记账、以 0 定稿——次日读回原数也不回头改它，
        差额记成次日的期初调整（漏读更正）。"""
        story = preset_run(Story())
        story.lost = ("兽肉",)
        story.restart(600).task("Daily_HomePage").at("begin", "daily", FULL)   # S5：当天最后一盘，漏读兽肉
        cut = len(story.events)
        next_morning(story)                                                    # S6：次日第一盘读回 1380
        for number, event in enumerate(story.events):
            event["event_id"] = f"e{number}"
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder)
            reports = output.report_dir(store, "42")
            output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=story.events[:cut])
            interim = json.loads((reports / "2026-11-14.json").read_text(encoding="utf-8"))
            output.generate_reports(store, "42", agent_dir=ROOT / "agent", hot=story.events)
            before = json.loads((reports / "2026-11-14.json").read_text(encoding="utf-8"))
            model = json.loads((reports / "2026-11-15.json").read_text(encoding="utf-8"))
        self.assertEqual((interim["status"], item(interim, "兽肉")["closing"]), ("interim", 0))
        self.assertEqual((before["status"], item(before, "兽肉")["closing"], before["balance"]["items"]["兽肉"]["quantity"]),
                         ("final", 0, 0))
        self.assertEqual((before["items"], before["statement"]), (interim["items"], interim["statement"]))   # 定稿不改数
        (meat,) = [row for row in model["adjustments"] if row["name"] == "兽肉"]
        self.assertEqual({key: meat[key] for key in ("before", "before_at", "delta", "opening", "reason")},
                         {"before": 0, "before_at": "S5", "delta": 1380, "opening": 1380, "reason": "suspect_zero"})
        self.assertTrue(any("兽肉" in notice["text"] and "漏读" in notice["text"] for notice in model["notices"]))
        self.assertIn("warn", {notice["level"] for notice in model["notices"] if "期初调整" in notice["text"]})

    def test_entry_page_in_install_folder(self):
        story = next_morning(preset_run(Story()))
        with tempfile.TemporaryDirectory() as folder:
            store, install = Path(folder) / "存档", Path(folder) / "安装目录"
            store.mkdir()
            install.mkdir()
            (store / "agent_save_data.json").write_text(json.dumps(
                {"arbitrage": {"inventory": {"events": story.events}}}, ensure_ascii=False), encoding="utf-8")
            # 不给 entry_dir（安卓、宿主指定存档目录）就不写入口
            self.assertIsNone(output.generate_reports(store, "0", agent_dir=ROOT / "agent", as_of=iso(story.now))["entry"])
            self.assertFalse((install / output.ENTRY_NAME).exists())
            result = output.generate_reports(store, "0", agent_dir=ROOT / "agent", as_of=iso(story.now), force=True,
                                             entry_dir=install)
            self.assertEqual(result["entry"], install / output.ENTRY_NAME)
            page = result["entry"].read_text(encoding="utf-8")
            latest = output.report_dir(store, "0").joinpath("latest.html").resolve().as_uri()
            self.assertIn(f"location.replace({json.dumps(latest)})", page)   # 只有一个存档：直接跳过去
            self.assertIn("默认存档", page)
            # 第二个存档出报表后，入口列出两个、不再自动跳
            (store / "agent_save_data_小号.json").write_text(json.dumps(
                {"arbitrage": {"inventory": {"events": story.events}}}, ensure_ascii=False), encoding="utf-8")
            output.generate_reports(store, "小号", agent_dir=ROOT / "agent", as_of=iso(story.now), entry_dir=install)
            page = (install / output.ENTRY_NAME).read_text(encoding="utf-8")
            self.assertNotIn("location.replace", page)
            self.assertIn("存档 小号", page)
            self.assertIn(output.report_dir(store, "小号").joinpath("index.html").resolve().as_uri(), page)
            # 安装目录写不进去：报表照出，只是没有入口
            original = output._write

            def read_only_install(path, text):
                if path.name == output.ENTRY_NAME:
                    raise OSError("只读")
                original(path, text)

            with patch.object(output, "_write", read_only_install):
                again = output.generate_reports(store, "0", agent_dir=ROOT / "agent", force=True, entry_dir=install)
            self.assertEqual((again["entry"], bool(again["days"])), (None, True))


if __name__ == "__main__":
    unittest.main()
