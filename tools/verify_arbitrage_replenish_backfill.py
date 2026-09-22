"""售罄候补扩列验证：真实规划/执行函数配合内存库存与成交回报，不连接游戏。"""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from action import arbitrage_replenish_buy as buying
from utils.arbitrage_replenish_plan import build_replenish_plan


DAY = "2026-09-17"


def make_plan(shops, *, target=10, gain=100, needed=1, owned=0, raw=10, budget=10000,
              purchased=(), observations=(), recipes=None):
    recipes = recipes or [("菜", target, gain, needed)]
    data = {"tonic_unit_price": 1, "recipes": {}, "shops": {
        shop: {"items": {"甲": {"price_reference": (price * 5 + 1) // 2, "daily_limit_reference": quantity}}}
        for shop, price, quantity in shops}}
    inventory = {"甲": owned}
    prices = {"甲": raw}
    entries = []
    for name, portions, extra, units in recipes:
        other = name + "配料"
        data["recipes"][name] = {"level": 1, "ingredients": {"甲": units, other: 1}}
        inventory[other] = portions
        prices[other] = 1
        prices[name] = raw * units + 2 + extra
        entries.append(NS(name=name, entry=name, enabled=True, reason=""))
    market = {"day": DAY, "complete": True, "items": [{"name": n, "peak_price": v} for n, v in prices.items()]}
    return build_replenish_plan(entries, inventory, market, data, day=DAY, budget=budget,
                                sell_names={e.name for e in entries}, purchased_items=purchased,
                                shop_observations=observations, profit_surrender_percent=10)


def run_purchases(plan, outcomes, *, dry_run=False):
    inventory = deepcopy(plan["input_quantities"])
    wallet = [10000]
    calls = []
    def execute(context, request):
        calls.append(deepcopy(request))
        assert len(calls) <= len(plan["purchase_offers"]), "Unbounded offer dispatch"
        outcome = outcomes.get(request["shop_name"], request["target"])
        if dry_run:
            return {"status": "prepared", "actual_quantity": 0, "actual_spent": 0}
        if isinstance(outcome, dict):
            return deepcopy(outcome)
        if isinstance(outcome, str):
            return {"status": outcome, "actual_quantity": None if outcome == "unknown" else 0,
                    "actual_spent": None if outcome == "unknown" else 0}
        quantity = min(request["target"], outcome)
        unit = request["max_unit_price"]
        owned = inventory[request["item_name"]]
        gold = wallet[0]
        spent = unit * quantity
        wallet[0] -= spent
        return {"status": "confirmed" if quantity == request["target"] else "partial",
                "actual_quantity": quantity, "actual_spent": spent, "owned": owned, "owned_after": owned + quantity,
                "unit_price": unit, "available": 99999, "remaining_stock": 99999 - quantity,
                "gold": gold, "gold_after": wallet[0], "selected": quantity, "quoted_total": spent,
                "inventory_source": "gold_confirmed", "remaining_stock_source": "calculated"}
    def save(quantities, *args):
        inventory.update(quantities)
        return True
    def invalidate(names, *args):
        for name in names:
            inventory.pop(name, None)
        return True
    context = NS(tasker=NS(stopping=False), set_anchor=lambda *a: True)
    with patch.object(buying, "sync_from_context", return_value=True), patch.object(buying, "ensure_shop"), \
         patch.object(buying, "execute_buy", side_effect=execute), \
         patch.object(buying.store, "market_day", return_value=DAY), \
         patch.object(buying.store, "get_replenish_inventory", side_effect=lambda _: {"quantities": dict(inventory)}), \
         patch.object(buying.store, "set_inventory_quantities", side_effect=save), \
         patch.object(buying.store, "invalidate_inventory_quantities", side_effect=invalidate), \
         patch.object(buying.mfaalog, "info"), patch.object(buying.mfaalog, "warning"):
        report = buying.execute_replenish_purchases(context, plan, bag_run_id="bag", dry_run=dry_run)
    return report, calls


class BackfillTests(unittest.TestCase):
    def test_sold_out_adds_unplanned_shop_then_stops_at_original_target(self):
        plan = make_plan([("便宜", 10, 10), ("候补", 12, 100), ("更贵", 20, 100)])
        self.assertEqual([r["shop_name"] for r in plan["requests"]], ["便宜"])
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual([(r["shop_name"], r["target"]) for r in calls], [("便宜", 10), ("候补", 10)])
        self.assertEqual((report["purchased"]["甲"], report["confirmed_spend"]), (10, 120))
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["unfilled_uses"], [])
        self.assertEqual(report["profit_adjustments"][0]["added_shops"], ["候补"])

    def test_sold_out_expands_already_planned_shop_within_its_supply(self):
        plan = make_plan([("便宜", 10, 5), ("候补", 12, 15)])
        self.assertEqual([r["target"] for r in plan["requests"]], [5, 5])
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual([r["target"] for r in calls], [5, 10])
        self.assertEqual(report["purchased"]["甲"], 10)

    def test_partial_receipt_moves_only_unfilled_quantity(self):
        plan = make_plan([("便宜", 10, 10), ("候补", 12, 100)])
        report, calls = run_purchases(plan, {"便宜": 3})
        self.assertEqual([r["target"] for r in calls], [10, 7])
        self.assertEqual(report["confirmed_spend"], 114)
        self.assertEqual(report["remaining_budget"], plan["budget"] - 114)
        self.assertEqual(report["unfilled_uses"], [])

    def test_profit_wall_stops_all_more_expensive_offers(self):
        plan = make_plan([("便宜", 10, 10), ("超限", 21, 10), ("更贵", 30, 10)])
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual([r["shop_name"] for r in calls], ["便宜"])
        self.assertEqual(report["confirmed_spend"], 0)
        self.assertEqual(report["unfilled_uses"][0]["quantity"], 10)
        self.assertEqual(report["profit_adjustments"][0]["detail"]["reason"], "profit_surrender_exceeded")

    def test_existing_eighteen_allow_two_expensive_onions(self):
        plan = make_plan([("便宜", 15, 2), ("候补", 45, 100), ("更贵", 100, 100)],
                         target=1, gain=1239, needed=20, owned=18, raw=24)
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual([(r["shop_name"], r["target"]) for r in calls], [("便宜", 2), ("候补", 2)])
        self.assertEqual(report["confirmed_spend"], 90)

    def test_bad_discount_at_one_shop_does_not_close_the_material(self):
        plan = make_plan([("便宜", 10, 10), ("候补", 12, 100)])
        outcome = {"status": "skipped", "actual_quantity": 0, "actual_spent": 0,
                   "unit_price": 99, "available": 10, "gold": 10000, "reason": "price_above_limit"}
        report, calls = run_purchases(plan, {"便宜": outcome})
        self.assertEqual([r["max_unit_price"] for r in calls], [10, 12])
        self.assertEqual(report["purchased"]["甲"], 10)

    def test_expansion_cannot_exceed_remaining_budget(self):
        plan = make_plan([("便宜", 10, 10), ("候补", 12, 100), ("更贵", 13, 100)], budget=100)
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual([r["target"] for r in calls], [10, 8])
        self.assertEqual(report["confirmed_spend"], 96)
        self.assertEqual(report["remaining_budget"], 4)
        self.assertEqual(report["unfilled_uses"][0]["quantity"], 2)

    def test_confirmed_regular_purchases_and_invalid_observations_never_return(self):
        exclusions = [("purchased", {"purchased": {("排除", "甲")}})]
        for remaining, unit, day in ((0, 12, DAY), (None, 12, DAY), (100, 13, DAY), (100, 12, "2026-09-16")):
            exclusions.append((str((remaining, unit, day)), {"observations": [
                {"shop_name": "排除", "item_name": "甲", "remaining": remaining, "unit_price": unit, "day": day}]}))
        for label, kwargs in exclusions:
            with self.subTest(case=label):
                plan = make_plan([("便宜", 10, 10), ("排除", 12, 100), ("合规", 14, 100)], **kwargs)
                report, calls = run_purchases(plan, {"便宜": "skipped"})
                self.assertEqual([r["shop_name"] for r in calls], ["便宜", "合规"])
                self.assertEqual(report["purchased"]["甲"], 10)

    def test_all_sold_out_offers_are_attempted_at_most_once(self):
        plan = make_plan([("一店", 10, 10), ("二店", 12, 100), ("三店", 14, 100)])
        report, calls = run_purchases(plan, {name: "skipped" for name in ("一店", "二店", "三店")})
        self.assertEqual([r["shop_name"] for r in calls], ["一店", "二店", "三店"])
        self.assertEqual(report["pending_requests"], [])
        self.assertEqual(report["confirmed_spend"], 0)

    def test_one_recipes_price_wall_does_not_stop_another_recipe(self):
        plan = make_plan([("便宜", 10, 4), ("候补", 20, 10)], recipes=[("菜", 2, 30, 1), ("后菜", 2, 200, 1)])
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual(calls[-1]["uses"], [{"recipe": "后菜", "quantity": 2}])
        self.assertEqual(report["purchased"]["甲"], 2)
        self.assertEqual(report["unfilled_uses"], [{"recipe": "菜", "item_name": "甲", "quantity": 2}])

    def test_expanded_shared_supply_and_budget_are_not_allocated_twice(self):
        plan = make_plan([("便宜", 10, 4), ("候补", 12, 3), ("超限", 40, 100)],
                         recipes=[("菜", 2, 100, 1), ("后菜", 2, 100, 1)])
        report, calls = run_purchases(plan, {"便宜": "skipped"})
        self.assertEqual(calls[-1]["target"], 3)
        self.assertEqual(calls[-1]["uses"], [{"recipe": "菜", "quantity": 2}, {"recipe": "后菜", "quantity": 1}])
        self.assertEqual(report["confirmed_spend"], 36)
        self.assertEqual(report["unfilled_uses"][0]["quantity"], 1)

    def test_unknown_receipt_never_triggers_replacement_purchase(self):
        plan = make_plan([("便宜", 10, 10), ("候补", 12, 100)])
        for status in ("unknown", "unexpected", "prepared"):
            with self.subTest(status=status):
                report, calls = run_purchases(plan, {"便宜": {
                    "status": status, "actual_quantity": 0, "actual_spent": 0}})
                self.assertEqual(len(calls), 1)
                self.assertEqual(report["unconfirmed_count"], 1)
                self.assertEqual(report["uncertain_budget"], 100)
                self.assertEqual(report["confirmed_spend"], 0)
                self.assertEqual(report["results"][0]["result"]["status"], "unknown")

    def test_prepare_only_does_not_expand_or_mark_trade(self):
        plan = make_plan([("便宜", 10, 10), ("候补", 12, 100)])
        report, calls = run_purchases(plan, {}, dry_run=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["status"], "prepared")
        self.assertEqual(report["confirmed_spend"], 0)

    def test_same_price_backups_follow_stable_shop_order(self):
        plan = make_plan([("甲店", 10, 10), ("乙店", 10, 10), ("丙店", 10, 10)])
        expected = sorted(("甲店", "乙店", "丙店"))
        report, calls = run_purchases(plan, {expected[0]: "skipped"})
        self.assertEqual([r["shop_name"] for r in calls], expected[:2])
        self.assertEqual(report["purchased"]["甲"], 10)


if __name__ == "__main__":
    unittest.main()
