"""补买每份料理利润比例：边界、逐量对照、配置及成交变化；不连接游戏或读写账号存档。"""

from copy import deepcopy
import json
from pathlib import Path
import random
import re
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from action import arbitrage_flow as flow
from action import arbitrage_replenish as controller
from action import arbitrage_replenish_buy as buying
from utils.arbitrage_replenish_plan import build_replenish_plan, order_purchase_requests
from utils.arbitrage_replenish_profit import select_purchase


DAY = "2026-09-17"
PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
INTERFACE = json.loads((ROOT / "assets/interface.json").read_text(encoding="utf-8"))


def offer(price, quantity, name="店"):
    return {"shop_name": name, "item_name": "甲", "unit_price": price, "remaining": quantity, "source": "test"}


def choose(*, needed=1, owned=0, raw=10, gain=30, offers=None, budget=10000,
           maximum=100, percent=10, committed=()):
    return select_purchase(needed_per=needed, owned=owned, material_value=raw, base_gain=gain,
                           offers=offers or [offer(10, 20)], budget=budget, max_portions=maximum,
                           percent=percent, committed=committed)


def tier_plan(percent=10, second_recipe=False, shared_recipe=False):
    data = {"tonic_unit_price": 1, "recipes": {"菜": {"level": 1, "ingredients": {"甲": 1, "乙": 1}}},
            "shops": {"便宜": {"items": {"甲": {"price_reference": 25, "daily_limit_reference": 10}}},
                      "昂贵": {"items": {"甲": {"price_reference": 50, "daily_limit_reference": 20}}}}}
    quantities = {"甲": 0, "乙": 50}
    prices = {"甲": 10, "乙": 1, "菜": 42}
    entries = [NS(name="菜", entry="菜", enabled=True, reason="")]
    if second_recipe:
        data["recipes"]["后菜"] = {"level": 1, "ingredients": {"丙": 1, "丁": 1}}
        data["shops"]["后店"] = {"items": {"丙": {"price_reference": 25, "daily_limit_reference": 5}}}
        quantities.update(丙=0, 丁=5)
        prices.update(丙=10, 丁=1, 后菜=42)
        entries.append(NS(name="后菜", entry="后菜", enabled=True, reason=""))
    if shared_recipe:
        data["recipes"]["后菜"] = {"level": 1, "ingredients": {"甲": 1, "丁": 1}}
        quantities.update(乙=5, 丁=50)
        prices.update(丁=1, 后菜=42)
        entries.append(NS(name="后菜", entry="后菜", enabled=True, reason=""))
    market = {"day": DAY, "complete": True, "items": [{"name": n, "peak_price": v} for n, v in prices.items()]}
    return build_replenish_plan(entries, quantities, market, data, day=DAY, budget=10000,
                                sell_names={row.name for row in entries}, profit_surrender_percent=percent)


class UnitProfitTests(unittest.TestCase):
    def test_zero_accepts_only_no_premium(self):
        for price, accepted in ((9, True), (10, True), (11, False)):
            result, detail = choose(offers=[offer(price, 20)], percent=0)
            self.assertEqual(result is not None, accepted)
            if not accepted:
                self.assertEqual(detail["reason"], "profit_surrender_exceeded")

    def test_user_example_exact_boundary_and_five_gold_left(self):
        for premium, accepted in ((150, True), (151, False), (1495, False)):
            result, _ = choose(raw=100, gain=1500, offers=[offer(100 + premium, 5)])
            self.assertEqual(result is not None, accepted)
            if result:
                self.assertEqual(result["estimated_gain"] // result["portions"], 1350)

    def test_hundred_still_requires_positive_gain(self):
        self.assertIsNone(choose(gain=30, offers=[offer(40, 10)], percent=100)[0])
        self.assertIsNotNone(choose(gain=30, offers=[offer(39, 10)], percent=100)[0])

    def test_chili_uses_recipe_quantity_and_requires_77_percent(self):
        for percent, accepted in ((10, False), (76, False), (77, True), (100, True)):
            result, _ = choose(needed=2, raw=66, gain=110, offers=[offer(108, 100)], percent=percent)
            self.assertEqual(result is not None, accepted)
            if result:
                self.assertEqual(result["premium_cost"] / result["portions"], 84)
                self.assertEqual(result["estimated_gain"] / result["portions"], 26)

    def test_existing_remainder_is_not_charged_purchase_premium(self):
        result, _ = choose(needed=2, owned=1, raw=10, gain=30, offers=[offer(13, 20)], percent=10)
        self.assertEqual((result["portions"], result["purchase_quantity"]), (1, 1))
        self.assertEqual(result["premium_cost"], 3)

    def test_ratio_cuts_inside_expensive_tier(self):
        plan = tier_plan()
        self.assertEqual([(r["shop_name"], r["target"]) for r in plan["requests"]], [("便宜", 10), ("昂贵", 4)])
        row = plan["allocations"][0]
        self.assertEqual((row["portions"], row["premium_cost"]), (14, 40))
        self.assertLessEqual(40 * 100, 14 * 30 * 10)
        self.assertGreater(50 * 100, 15 * 30 * 10)

    def test_cheaper_purchase_savings_do_not_expand_premium_allowance(self):
        result, _ = choose(gain=10, offers=[offer(1, 1, "便宜"), offer(20, 1, "昂贵")], percent=10)
        self.assertEqual(result["portions"], 1)

    def test_recipes_cannot_borrow_another_recipes_allowance(self):
        plan = tier_plan(second_recipe=True)
        requests = [r for r in plan["requests"] if r["item_name"] == "甲"]
        self.assertEqual(sum(r["target"] for r in requests), 14)
        self.assertEqual(plan["cook_today_candidates"], ["菜", "后菜"])

    def test_random_tier_boundaries_match_exhaustive_unit_calculation(self):
        rng = random.Random(170926)
        for case in range(500):
            needed = rng.randint(1, 5)
            owned = rng.randrange(needed)
            raw = rng.randint(1, 20)
            gain = rng.randint(1, 60)
            percent = rng.choice((0, 1, 10, 33, 77, 100))
            budget = rng.randint(1, 600)
            maximum = rng.randint(1, 20)
            offers = [offer(rng.randint(1, 60), rng.randint(1, 15), str(i)) for i in range(rng.randint(1, 4))]
            committed = ([{"quantity": rng.randint(1, 5), "unit_price": rng.randint(1, 35)}]
                         if case % 3 == 0 else [])
            expected = None
            for portions in range(1, maximum + 1):
                missing = portions * needed - owned - sum(row["quantity"] for row in committed)
                if missing < 0:
                    continue
                bought = list(committed)
                for row in sorted(offers, key=lambda x: (x["unit_price"], x["shop_name"])):
                    count = min(missing, row["remaining"])
                    bought.append({"quantity": count, "unit_price": row["unit_price"]})
                    missing -= count
                if missing:
                    continue
                cost = sum(row["quantity"] * row["unit_price"] for row in bought)
                premium = sum(row["quantity"] * max(0, row["unit_price"] - raw) for row in bought)
                actual_gain = portions * gain - sum(row["quantity"] * (row["unit_price"] - raw) for row in bought)
                if cost <= budget and actual_gain > 0 and premium * 100 <= portions * gain * percent:
                    expected = max(expected or (0, 0), (actual_gain, portions))
            result, _ = choose(needed=needed, owned=owned, raw=raw, gain=gain, offers=offers,
                               budget=budget, maximum=maximum, percent=percent, committed=committed)
            actual = (result["estimated_gain"], result["portions"]) if result else None
            self.assertEqual(actual, expected, f"case={case}")


class ConfigurationTests(unittest.TestCase):
    def test_ui_defaults_validation_and_python_reading_agree(self):
        config = INTERFACE["option"]["补买利润让出比例"]
        field = config["inputs"][0]
        branch = next(c for c in INTERFACE["option"]["料理缺料补买"]["cases"] if c["name"] == "Yes")
        self.assertIn("补买利润让出比例", branch["option"])
        self.assertEqual(field["default"], "10")
        self.assertEqual(PIPE["Arbitrage_Cooking_Replenish"]["attach"]["profit_surrender_percent"], 10)
        self.assertEqual(config["pipeline_override"]["Arbitrage_Cooking_Replenish"]["attach"]["profit_surrender_percent"],
                         "{利润让出比例}")
        for value in ("0", "10", "100"):
            self.assertIsNotNone(re.fullmatch(field["verify"], value))
            context = NS(get_node_object=lambda _: NS(attach={"profit_surrender_percent": value}))
            self.assertEqual(flow.replenish_profit_surrender(context), int(value))
        for value in ("-1", "101", "10.5", "", "abc", True, 10.0):
            context = NS(get_node_object=lambda _: NS(attach={"profit_surrender_percent": value}))
            with self.subTest(value=value), self.assertRaises(ValueError):
                flow.replenish_profit_surrender(context)
        self.assertIn("每份", config["description"])
        self.assertIn("原价减60%", config["description"])

    def test_profit_callback_uses_per_portion_amounts(self):
        plan = tier_plan()
        with patch.object(controller.mfaalog, "info") as log:
            controller._log_profit_decisions(plan)
        self.assertIn("每份基准多赚30", log.call_args.args[0])
        self.assertIn("允许让出10%=3", log.call_args.args[0])
        self.assertIn("预计每份补买溢价2.86", log.call_args.args[0])

    def test_both_controller_passes_use_the_selected_percent(self):
        plan = tier_plan(77)
        context = NS(tasker=NS(stopping=False), get_node_data=lambda _: {}, set_anchor=lambda *a: True,
                     run_task=lambda _: NS(status=NS(succeeded=True)))
        with patch.object(controller, "sync_from_context", return_value=True), \
             patch.object(controller, "cooking_stage_active", return_value=True), \
             patch.object(controller, "get_bag_scan_run", return_value="bag"), \
             patch.object(controller, "replenishment_entries", return_value=[NS(name="菜", enabled=True)]), \
             patch.object(controller, "completed_purchase_items", return_value=set()), \
             patch.object(controller.store, "get_replenish_inventory", return_value={"quantities": {"甲": 0}}), \
             patch.object(controller.store, "get_market_snapshot", return_value={"complete": True}), \
             patch.object(controller, "build_replenish_plan", return_value=plan) as build, \
             patch.object(controller, "ensure_shop"), patch.object(controller, "_gold", return_value=10000), \
             patch.object(controller, "execute_replenish_purchases", return_value={"status": "prepared"}), \
             patch.object(controller.mfaalog, "info"):
            controller.run_replenishment(context, 1, {"dry_run": True, "profit_surrender_percent": 77})
        self.assertEqual(build.call_count, 2)
        self.assertEqual([c.kwargs["profit_surrender_percent"] for c in build.call_args_list], [77, 77])


class ReceiptTests(unittest.TestCase):
    def test_execution_sorts_same_material_by_unit_price_after_merging(self):
        plan = tier_plan(second_recipe=True)
        plan["requests"][0], plan["requests"][1] = plan["requests"][1], plan["requests"][0]
        with patch.object(buying.store, "market_day", return_value=DAY):
            requests = buying._requests(plan)
        self.assertEqual([(row["item_name"], row["shop_name"]) for row in requests],
                         [("甲", "便宜"), ("甲", "昂贵"), ("丙", "后店")])
        self.assertEqual(order_purchase_requests(requests), requests)

    def run_purchases(self, first, second_recipe=False, shared_recipe=False):
        plan = tier_plan(second_recipe=second_recipe, shared_recipe=shared_recipe)
        inventory = deepcopy(plan["input_quantities"])
        wallet = [10000]
        calls = []
        def execute(context, request):
            calls.append(deepcopy(request))
            if len(calls) == 1 and isinstance(first, str):
                return {"status": first, "actual_quantity": None if first == "unknown" else 0,
                        "actual_spent": None if first == "unknown" else 0}
            quantity = first if len(calls) == 1 else request["target"]
            unit = request["max_unit_price"]
            owned = inventory[request["item_name"]]
            gold = wallet[0]
            spent = unit * quantity
            wallet[0] -= spent
            return {"status": "confirmed" if quantity == request["target"] else "partial",
                    "actual_quantity": quantity, "actual_spent": spent, "owned": owned,
                    "owned_after": owned + quantity, "unit_price": unit, "available": 20,
                    "remaining_stock": 20 - quantity, "gold": gold, "gold_after": wallet[0],
                    "selected": quantity, "quoted_total": spent,
                    "inventory_source": "gold_confirmed", "remaining_stock_source": "calculated"}
        def save(quantities, *args):
            inventory.update(quantities)
            return True
        def invalidate(names, *args):
            for name in names:
                inventory.pop(name, None)
            return True
        context = NS(tasker=NS(stopping=False), set_anchor=lambda *args: True)
        with patch.object(buying, "sync_from_context", return_value=True), patch.object(buying, "ensure_shop"), \
             patch.object(buying, "execute_buy", side_effect=execute), \
             patch.object(buying.store, "market_day", return_value=DAY), \
             patch.object(buying.store, "get_replenish_inventory", side_effect=lambda _: {"quantities": dict(inventory)}), \
             patch.object(buying.store, "set_inventory_quantities", side_effect=save), \
             patch.object(buying.store, "invalidate_inventory_quantities", side_effect=invalidate), \
             patch.object(buying.mfaalog, "info"):
            report = buying.execute_replenish_purchases(context, plan, bag_run_id="bag", dry_run=False)
        return report, calls

    def test_partial_cheap_purchase_reduces_expensive_request(self):
        report, calls = self.run_purchases(5)
        self.assertEqual([c["target"] for c in calls], [10, 2])
        self.assertEqual(report["confirmed_spend"], 90)
        self.assertEqual(report["purchased"]["甲"], 7)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["profit_adjustments"][0]["after"], 2)

    def test_missing_cheap_purchase_cancels_expensive_request(self):
        report, calls = self.run_purchases("not_found")
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["confirmed_spend"], 0)
        self.assertEqual(report["pending_requests"], [])

    def test_unknown_purchase_blocks_dependents_but_keeps_other_recipe(self):
        report, calls = self.run_purchases("unknown", second_recipe=True)
        self.assertEqual([c["item_name"] for c in calls], ["甲", "丙"])
        self.assertEqual(report["unconfirmed_count"], 1)
        self.assertEqual(report["purchased"], {"丙": 5})

    def test_full_receipts_do_not_replan_or_expand_targets(self):
        report, calls = self.run_purchases(10)
        self.assertEqual([c["target"] for c in calls], [10, 4])
        self.assertEqual(report["status"], "completed")
        self.assertNotIn("profit_adjustments", report)

    def test_partial_merged_receipt_does_not_lend_first_recipes_credit_to_second(self):
        plan = tier_plan(shared_recipe=True)
        self.assertEqual(plan["requests"][0]["uses"], [{"recipe": "菜", "quantity": 5}, {"recipe": "后菜", "quantity": 5}])
        self.assertEqual(plan["requests"][1]["target"], 2)
        report, calls = self.run_purchases(7, shared_recipe=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["confirmed_spend"], 70)
        self.assertEqual(report["profit_adjustments"][0]["recipe"], "后菜")
        self.assertEqual(report["profit_adjustments"][0]["after"], 0)


if __name__ == "__main__":
    unittest.main()
