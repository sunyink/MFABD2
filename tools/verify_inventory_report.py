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
                "scope": list(scopes), "complete": True, "termination": "list_bottom", "pages": 7,
                "quantities": {name: quantity for name, quantity in seen.items() if quantity > 0},
                "absent": [name for name, quantity in seen.items() if quantity == 0],
                "unknown": {name: "看不清" for name in self.unknown if SCOPE[name] in scopes},
                "origin": {"mark": mark, "type": kind, "node": node, "task": task, "session": self.session}})
            self.now += 8
            self.events.append({"kind": "currency_reading", "scan_id": scan_id, "observed_at": iso(self.now),
                                "label": f"记账·{mark}", **self.money})
            self.unknown = ()
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

    def test_collect_without_end_mark_is_abnormal_but_settled(self):
        story = Story()
        story.task("Collect_StartGame_HomePage").at("begin", "collect", FULL).work(300, 兽肉=100)
        story.task("Arbitrage_Start").at("begin", "shop", ("A",)).work(60)
        story.at("end", "shop", ("A",))
        first, second = story.end().ledger()["intervals"]
        self.assertEqual((first["kind"], first["flags"]), ("collect", ["abnormal_end"]))
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
        story = preset_run(Story())
        interim = story.report()
        self.assertEqual((interim["status"], [col["key"] for col in interim["cols"]]),
                         ("interim", ["daily", "collect", "shop"]))
        self.assertEqual(item(interim, "精炼石")["closing_at"], "S2")   # 当天最后一次读到它的盘点
        final = next_morning(story).report()
        self.assertEqual((final["status"], [col["key"] for col in final["cols"]]),
                         ("final", ["daily", "collect", "shop", "outside"]))
        self.assertIn("cross_day", final["segments"][-1]["flags"])
        self.assertTrue(all(check["ok"] for check in final["checks"]), final["checks"])
        self.assertIsNone(story.report("2026-11-13"))
        self.assertEqual(story.report("2026-11-15")["status"], "interim")

    def test_columns_hold_changes_not_readings(self):
        model = next_morning(preset_run(Story())).report()
        meat = item(model, "兽肉")
        self.assertEqual((meat["opening"], meat["deltas"], meat["closing"]),
                         (1000, {"daily": 0, "collect": 400, "shop": -20, "outside": 0}, 1380))
        stone = item(model, "精炼石")
        # 吸取、商店与隔夜那段的两头盘点不都扫养成材料：这几格是「不扫」，合起来没变所以也没有跨区间的数
        self.assertEqual((stone["deltas"], stone["gaps"], stone["spans"]),
                         ({"daily": 3}, ["collect", "shop", "outside"], []))
        gold = item(model, "金币")
        self.assertEqual(gold["deltas"], {"daily": 5000, "collect": 0, "shop": 190_000 - 1700 - 700, "outside": 0})
        self.assertEqual(model["statement"]["totals"]["cash"], gold["total"])

    def test_ticket_changed_while_unscanned_goes_to_a_span(self):
        story = preset_run(Story())
        story.stock["抽抽乐券"] += 0   # 日常里 +1 发生在第二盘之前，已在日常列
        model = next_morning(story.work(0, 抽抽乐券=2)).report()   # 结束游戏之后手动得了 2 张
        ticket = item(model, "抽抽乐券")
        self.assertEqual(ticket["deltas"], {"daily": 1})
        self.assertEqual([(span["cols"], span["delta"], span["reason"]) for span in ticket["spans"]],
                         [(["collect", "shop", "outside"], 2, "out_of_scope")])
        self.assertEqual(ticket["closing"], 5)

    def test_rerun_of_one_daily_lands_in_the_daily_column(self):
        """先缺一个日常跑完全套，再回去单跑那个日常：它的变化仍在日常列，隔夜那段归当天。"""
        story = preset_run(Story())
        story.restart(1800).task("Pass_HomePage").at("begin", "daily", FULL).work(精炼石=7)
        model = next_morning(story).report()
        self.assertEqual([(col["key"], col["runs"]) for col in model["cols"]],
                         [("daily", 2), ("collect", 1), ("shop", 1), ("outside", 1)])
        self.assertEqual(item(model, "精炼石")["deltas"]["daily"], 10)
        self.assertIn("unsettled", model["cols"][0]["flags"])
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

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

    def test_shop_reconciliation_leaves_regular_purchase_as_residual(self):
        model = preset_run(Story()).report()
        rows = {row["name"]: row for row in model["shop"]["rows"]}
        self.assertEqual((rows["米"]["net"], rows["米"]["replenish"], rows["米"]["cooking"], rows["米"]["residual"]),
                         (80, 100, -60, 40))
        self.assertEqual((rows["兽肉"]["cooking"], rows["兽肉"]["residual"]), (-20, 0))
        self.assertEqual((rows["包饭"]["net"], rows["包饭"]["sales"], rows["包饭"]["cooking"], rows["包饭"]["residual"]),
                         (-5, -25, 20, 0))
        self.assertEqual(model["shop"]["gold"], {"net": 187_600, "sales": 190_000, "replenish": -1700, "residual": -700,
                                                 "unconfirmed_sales": 0})
        self.assertEqual(model["shop"]["mismatch"], [])

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
        self.assertTrue(all(check["ok"] for check in model["checks"]), model["checks"])

    def test_first_day_and_tail_are_announced(self):
        story = Story()
        story.task("Daily_HomePage").at("begin", "daily", FULL).work(精炼石=1)
        model = story.report()
        self.assertTrue(model["first_scan"])
        self.assertEqual((model["segments"], [row["name"] for row in model["items"]]), ([], []))
        self.assertTrue(any("没盘掉的活动（日常）" in notice["text"] for notice in model["notices"]))


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


if __name__ == "__main__":
    unittest.main()
