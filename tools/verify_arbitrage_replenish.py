"""收藏核对、采购最终名单与补买供给回归；不连接游戏、不读写账号存档。

运行：python -B tools/verify_arbitrage_replenish.py -v
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from action import arbitrage_buy_list as buy
from action.arbitrage_buy_precise import BuyQuantityAdjuster
from action import arbitrage_replenish as replenish
from action import arbitrage_replenish_buy as replenish_buy
from action import arbitrage_result as result
from action import arbitrage_sell_batch as sale
from action import shop_buy_fav_controller as favorites
from utils import arbitrage_purchase_lists as lists
from utils.arbitrage_replenish_data import discounted_purchase_price, load_replenish_data
from utils.arbitrage_replenish_plan import build_replenish_plan
from utils.persistent_store import PersistentStore


DAY = "2026-09-15"
PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
DATA = load_replenish_data()


class Reader:
    def __init__(self):
        self.nodes = deepcopy(PIPE)
        self.tasker = NS(stopping=False)
        self.anchors = {}

    def set_anchor(self, name, target):
        self.anchors[name] = target
        return True

    def get_anchor(self, name):
        return self.anchors.get(name)

    def get_node_object(self, name):
        return NS(attach=self.nodes[name].get("attach", {}))

    def get_node_data(self, name):
        return self.nodes[name]

    def override_pipeline(self, values):
        for name, value in values.items():
            self.nodes[name].update(value)
        return True


def salt_plan(purchased=(), observations=(), budget=1000000):
    recipe = DATA["recipes"]["香草牛排"]
    inventory = {name: 100000 for name in recipe["ingredients"]}
    inventory["盐"] = 0
    entry = NS(name="香草牛排", enabled=True, reason="", entry="test_recipe")
    market = {"day": DAY, "complete": True, "items": [
        {"name": name, "peak_price": value["peak_reference"]}
        for name, value in {**DATA["materials"], **DATA["recipes"]}.items()]}
    return build_replenish_plan([entry], inventory, market, DATA, day=DAY, budget=budget,
                                sell_names={entry.name}, purchased_items=purchased,
                                shop_observations=observations)


class PurchaseSupplyTests(unittest.TestCase):
    def setUp(self):
        self.context = Reader()
        self.argv = NS(task_detail=NS(task_id=99), node_name="Arbitrage_Buy_Select_End",
                       custom_action_param={})
        for p in (patch.dict(lists._RUNS, clear=True),
                  patch.object(PersistentStore, "_current_account_id", "test"),
                  patch.object(buy, "sync_from_context", return_value=True),
                  patch.object(buy.store, "get_purchase_alignments", return_value={}),
                  patch.object(buy.store, "market_day", return_value=DAY),
                  patch.object(buy.mfaalog, "info")):
            p.start()
            self.addCleanup(p.stop)

    def prepare(self, complete=True):
        self.assertTrue(buy.ArbitrageBuyListPrepare().run(self.context, self.argv))
        run = lists.get_purchase_run(99)
        run["pending"].clear()
        if complete:
            self.assertTrue(buy.ArbitrageBuyComplete().run(self.context, self.argv))
        return run

    def custom(self, card, selected):
        self.context.nodes[lists.PREPARE_NODE]["attach"]["custom_enabled"] = True
        node = lists.load_purchase_catalog()[card]["custom_node"]
        self.context.nodes[node]["enabled"] = True
        self.context.nodes[node]["attach"] = {
            name: name in selected for name in lists.load_purchase_catalog()[card]["items"]}

    def test_default_confirmed_salt_shops_are_not_replenished(self):
        self.prepare()
        purchased = lists.completed_purchase_items(99, DAY)
        sold = {"三国同盟", "被遗忘的战争", "试炼之路"}
        self.assertTrue({(shop, "盐") for shop in sold} <= purchased)
        baseline = {row["shop_name"] for row in salt_plan()["requests"]}
        remaining = {row["shop_name"] for row in salt_plan(purchased)["requests"]}
        self.assertTrue(sold <= baseline)
        self.assertEqual(remaining, baseline - sold)

    def test_interface_custom_removal_restores_that_shop_offer(self):
        self.custom("S16:三国同盟", set())
        self.prepare()
        purchased = lists.completed_purchase_items(99, DAY)
        self.assertNotIn(("三国同盟", "盐"), purchased)
        self.assertIn("三国同盟", {row["shop_name"] for row in salt_plan(purchased)["requests"]})

    def test_interface_addition_excludes_newly_selected_offer(self):
        self.custom("S5:沙漠之花", {"盐"})
        self.prepare()
        self.assertIn(("沙漠之花", "盐"), lists.completed_purchase_items(99, DAY))

    def test_disabled_outer_custom_ignores_hidden_selection(self):
        self.custom("S16:三国同盟", set())
        self.context.nodes[lists.PREPARE_NODE]["attach"]["custom_enabled"] = False
        self.prepare()
        self.assertIn(("三国同盟", "盐"), lists.completed_purchase_items(99, DAY))

    def test_preparation_alone_and_other_task_or_day_do_not_exclude(self):
        self.assertEqual(lists.completed_purchase_items(99, DAY), set())
        self.prepare(complete=False)
        self.assertEqual(lists.completed_purchase_items(99, DAY), set())
        self.assertTrue(buy.ArbitrageBuyComplete().run(self.context, self.argv))
        self.assertEqual(lists.completed_purchase_items(100, DAY), set())
        self.assertEqual(lists.completed_purchase_items(99, "2026-09-16"), set())

    def test_unverified_cards_are_not_assumed_purchased(self):
        run = self.prepare()
        run["failed_cards"]["S16:三国同盟"] = "unreadable"
        run["pending"].add("S17:试炼之路")
        purchased = lists.completed_purchase_items(99, DAY)
        self.assertNotIn(("三国同盟", "盐"), purchased)
        self.assertNotIn(("试炼之路", "盐"), purchased)
        self.assertIn(("被遗忘的战争", "盐"), purchased)

    def test_changed_account_cannot_reuse_purchase_evidence(self):
        self.prepare()
        PersistentStore._current_account_id = "other"
        with self.assertRaises(ValueError):
            lists.completed_purchase_items(99, DAY)
        self.assertEqual(lists.completed_purchase_items(99, DAY), set())

    def test_bad_custom_card_preserves_other_cards_and_current_favorites(self):
        card = "S16:三国同盟"
        self.custom(card, set())
        definition = lists.load_purchase_catalog()[card]
        del self.context.nodes[definition["custom_node"]]["attach"]["盐"]
        self.assertTrue(buy.ArbitrageBuyListPrepare().run(self.context, self.argv))
        run = lists.get_purchase_run(99)
        self.assertNotIn(card, run["table"])
        self.assertIn(card, run["failed_cards"])
        self.assertIn("S17:试炼之路", run["table"])
        self.assertFalse(self.context.nodes[definition["selector"]]["enabled"])
        with patch.object(buy.store, "invalidate_purchase_alignments", return_value=True) as invalidate:
            self.assertTrue(buy.ArbitrageBuyScanPrepare().run(self.context, self.argv))
        self.assertNotIn(card, invalidate.call_args.args[0])
        self.assertFalse(self.context.nodes[definition["selector"]]["enabled"])
        run["pending"].clear()
        self.assertFalse(buy.ArbitrageBuyReady().run(self.context, self.argv))
        self.assertTrue(buy.ArbitrageBuyComplete().run(self.context, self.argv))
        purchased = lists.completed_purchase_items(99, DAY)
        self.assertNotIn(("三国同盟", "盐"), purchased)
        self.assertIn(("试炼之路", "盐"), purchased)

    def test_unreadable_custom_node_does_not_silently_restore_defaults(self):
        self.custom("S16:三国同盟", set())
        node = lists.load_purchase_catalog()["S16:三国同盟"]["custom_node"]
        del self.context.nodes[node]
        self.assertTrue(buy.ArbitrageBuyListPrepare().run(self.context, self.argv))
        self.assertNotIn("S16:三国同盟", lists.get_purchase_run(99)["table"])

    def test_broken_default_row_is_isolated_and_empty_custom_is_valid(self):
        catalog = lists.load_purchase_catalog()
        defaults = deepcopy(self.context.nodes[lists.DATA_NODE]["attach"])
        defaults["S1:血骑士"] = None
        table, errors = lists.resolve_purchase_table(defaults, catalog, {
            "S16:三国同盟": {item: False for item in catalog["S16:三国同盟"]["items"]}})
        self.assertEqual(set(errors), {"S1:血骑士"})
        self.assertNotIn("S1:血骑士", table)
        self.assertEqual(table["S16:三国同盟"], set())

    def test_completion_without_record_or_sync_still_returns_to_cleanup(self):
        self.assertTrue(buy.ArbitrageBuyComplete().run(self.context, self.argv))
        self.prepare(complete=False)
        with patch.object(buy, "sync_from_context", return_value=False):
            self.assertTrue(buy.ArbitrageBuyComplete().run(self.context, self.argv))
        self.assertEqual(lists.completed_purchase_items(99, DAY), set())

    def test_ready_exception_reports_unverified_for_pipeline(self):
        with patch.object(buy, "get_purchase_run", side_effect=ValueError("bad state")):
            self.assertFalse(buy.ArbitrageBuyReady().run(self.context, self.argv))

    def test_fresh_shop_observation_overrides_purchase_inference(self):
        self.prepare()
        observed = [{"shop_name": "三国同盟", "item_name": "盐", "remaining": 50,
                     "unit_price": 1, "day": DAY}]
        rows = salt_plan(lists.completed_purchase_items(99, DAY), observed)["requests"]
        self.assertEqual(next(row["target"] for row in rows if row["shop_name"] == "三国同盟"), 50)

    def check_controller_exclusions(self, unavailable=False):
        self.prepare()
        purchased = lists.completed_purchase_items(99, DAY)
        plan = salt_plan(purchased)
        self.context.run_task = lambda name: NS(status=NS(succeeded=True))
        with patch.object(replenish, "sync_from_context", return_value=True), \
             patch.object(replenish, "completed_purchase_items", return_value=purchased,
                          side_effect=ValueError("purchase context lost") if unavailable else None), \
             patch.object(replenish, "cooking_stage_active", return_value=True), \
             patch.object(replenish, "get_bag_scan_run", return_value="bag"), \
             patch.object(replenish.store, "get_replenish_inventory", return_value={"quantities": {"盐": 0}}), \
             patch.object(replenish, "replenishment_entries", return_value=[NS(name="香草牛排", enabled=True)]), \
             patch.object(replenish.store, "get_market_snapshot", return_value={"complete": True}), \
             patch.object(replenish, "build_replenish_plan", return_value=plan) as build, \
             patch.object(replenish, "ensure_shop"), patch.object(replenish, "_gold", return_value=1000000), \
             patch.object(replenish, "execute_replenish_purchases", return_value={"status": "prepared"}):
            result = replenish.run_replenishment(self.context, 99, {"dry_run": True})
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(self.context.get_anchor("Replenish_ShopEntry"), "Arbitrage_Merchant_Entry")
        self.assertEqual(build.call_count, 2)
        ceiling = sum(discounted_purchase_price(item["price_reference"]) * min(item["daily_limit_reference"], 99999)
                      for shop in DATA["shops"].values() for item in shop["items"].values())
        self.assertEqual(build.call_args_list[0].kwargs["budget"], ceiling)
        self.assertEqual(build.call_args_list[1].kwargs["budget"], 1000000)
        expected = set() if unavailable else purchased
        self.assertTrue(all(call.kwargs["purchased_items"] == expected for call in build.call_args_list))

    def test_controller_supplies_same_exclusions_to_both_planning_passes(self):
        self.check_controller_exclusions()

    def test_unavailable_purchase_evidence_does_not_stop_replenishment(self):
        self.check_controller_exclusions(unavailable=True)


class DiscountPurchaseTests(unittest.TestCase):
    PURCHASED = {(shop, "盐") for shop in ("三国同盟", "被遗忘的战争", "试炼之路")}

    def test_reference_salt_plan_matches_six_discounted_receipts(self):
        plan = salt_plan(self.PURCHASED)
        self.assertEqual(sum(row["target"] for row in plan["requests"]), 2400)
        self.assertEqual(plan["estimated_spend"], 19600)
        self.assertEqual(len(plan["requests"]), 6)
        for row in plan["requests"]:
            with self.subTest(shop=row["shop_name"]):
                unit = 14 if row["shop_name"] == "铁假面" else 7
                self.assertEqual((row["target"], row["max_unit_price"], row["budget"]), (400, unit, 400 * unit))
                self.assertEqual(row["quote_source"], "discounted_reference")
        self.assertEqual(DATA["shops"]["沙漠之花"]["items"]["盐"]["price_reference"], 18)
        self.assertEqual(DATA["shops"]["铁假面"]["items"]["盐"]["price_reference"], 36)

    def test_budget_uses_discount_before_selecting_quantity(self):
        for budget, quantity in ((19600, 2400), (2800, 400), (140, 20)):
            with self.subTest(budget=budget):
                plan = salt_plan(self.PURCHASED, budget=budget)
                self.assertEqual(sum(row["target"] for row in plan["requests"]), quantity)
                self.assertEqual(plan["estimated_spend"], budget)
                self.assertEqual(plan["budget_remaining"], 0)

    def test_round_unit_price_before_multiplying_quantity(self):
        for original, expected in ((8, 3), (12, 4), (18, 7), (36, 14), (270, 108)):
            with self.subTest(original=original):
                self.assertEqual(discounted_purchase_price(original), expected)
        for invalid in (True, 0, -18, 18.0, "18"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                discounted_purchase_price(invalid)

    def test_observed_price_is_not_discounted_twice(self):
        for price in (7, 6):
            observed = [{"shop_name": "沙漠之花", "item_name": "盐", "remaining": 400,
                         "unit_price": price, "day": DAY}]
            plan = salt_plan(self.PURCHASED, observed)
            row = next(row for row in plan["requests"] if row["shop_name"] == "沙漠之花")
            self.assertEqual((row["max_unit_price"], row["budget"]), (price, price * 400))
            self.assertEqual(row["quote_source"], "observed")

    def test_observation_cannot_raise_the_agreed_discount_limit(self):
        for price in (8, 18):
            observed = [{"shop_name": "沙漠之花", "item_name": "盐", "remaining": 400,
                         "unit_price": price, "day": DAY}]
            plan = salt_plan(self.PURCHASED, observed)
            self.assertNotIn("沙漠之花", {row["shop_name"] for row in plan["requests"]})
            issue = next(row for row in plan["offer_issues"] if row["shop_name"] == "沙漠之花")
            self.assertEqual((issue["reason"], issue["max_unit_price"]), ("purchase_discount_not_met", 7))

    def test_precise_buy_enforces_generated_discount_limit_before_selecting_max(self):
        request = next(row for row in salt_plan(self.PURCHASED)["requests"] if row["shop_name"] == "沙漠之花")
        for unit in (7, 8, 18):
            with self.subTest(unit=unit):
                adjuster = BuyQuantityAdjuster.__new__(BuyQuantityAdjuster)
                adjuster.request = request
                adjuster.check_running = Mock()
                adjuster.action = Mock()
                adjuster.adjust = Mock()
                initial = (0, 400, 40000, unit)
                adjuster.read = Mock(side_effect=[(initial, 1), (initial, 1), 400,
                                                 ((0, 400, 40000, unit * 400), 400)])
                result = adjuster.prepare()
                if unit == 7:
                    self.assertEqual((result["status"], result["selected"], result["quoted_total"]),
                                     ("ready", 400, 2800))
                else:
                    self.assertEqual(result["status"], "skipped")
                    self.assertIn(f"实际单价{unit}超过本批单价上限7", result["reason"])
                    adjuster.action.assert_called_once_with("min_node")
                    adjuster.adjust.assert_not_called()

    def test_discounted_chili_unlocks_profitable_recipe(self):
        name = "甜辣鲜虾"
        entry = NS(name=name, enabled=True, reason="", entry="test_recipe")
        market = {"day": DAY, "complete": True, "items": [
            {"name": item, "peak_price": price} for item, price in
            {name: 786, "虾": 39, "小麦": 7, "料酒": 120, "甜辣酱": 66}.items()]}
        supply = {**DATA, "shops": {"铁假面": DATA["shops"]["铁假面"]}}
        inventory = {"虾": 5259, "小麦": 16553, "料酒": 516, "甜辣酱": 0}
        plan = build_replenish_plan([entry], inventory, market, supply, day=DAY, budget=10800, sell_names={name},
                                    profit_surrender_percent=100)
        self.assertEqual(plan["cook_today_candidates"], [name])
        self.assertEqual((plan["requests"][0]["target"], plan["requests"][0]["max_unit_price"]), (100, 108))
        self.assertEqual((plan["estimated_spend"], plan["allocations"][0]["portions"],
                          plan["allocations"][0]["estimated_gain"]), (10800, 50, 1300))
        self.assertEqual(DATA["tonic_unit_price"], 45)

    def test_regular_purchase_navigation_keeps_discount_entry(self):
        for name in ("Arbitrage_Buy_Open", "Arbitrage_Buy_CurrentFavorites"):
            self.assertIn("[JumpBack]Arbitrage_Merchant_Entry", PIPE[name]["next"])
            self.assertNotIn("[JumpBack]Arbitrage_Merchant_NoDiscount_Entry", PIPE[name]["next"])


class FavoriteAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.controller = favorites.ShopBuyFavController()
        self.controller.cfg = {"bind_dx_min": 5, "bind_dx_max": 40, "bind_dy_max": 15}
        self.controller._scan_issues = []

    def bind(self, names):
        stars = [{"box": [100, y, 20, 20], "cx": 110, "cy": y + 10,
                  "right_x": 120, "color": "gray"} for y in (100, 200)]
        labels = [{"name": name, "box": [130, y, 60, 20], "left_x": 130, "cy": y + 10}
                  for name, y in names]
        return self.controller._bind_star_to_name(stars, labels)

    def test_same_name_at_two_positions_adjusts_both_stars(self):
        entities = self.bind([("盐", 100), ("盐", 200)])
        self.assertTrue(self.controller._complete_page(entities))
        actions = self.controller._decide_actions(entities, {"盐"})
        self.assertEqual([(row["action"], row["star_cy"]) for row in actions],
                         [("light", 110), ("light", 210)])

    def test_missing_or_ambiguous_name_cannot_save_alignment(self):
        for labels in ([('盐', 100)], [('盐', 100), ('糖', 100), ('盐', 200)]):
            with self.subTest(labels=labels):
                self.controller._scan_issues = []
                self.assertFalse(self.controller._complete_page(self.bind(labels)))

    def test_failed_card_does_not_block_other_cards_or_count_as_purchased(self):
        with patch.dict(lists._RUNS, clear=True), \
             patch.object(PersistentStore, "_current_account_id", "test"), \
             patch.object(favorites, "sync_from_context", return_value=True), \
             patch.object(buy, "sync_from_context", return_value=True), \
             patch.object(favorites, "invalidate_purchase_alignment", return_value=True) as invalidate, \
             patch.object(favorites, "save_purchase_alignment", return_value=True) as save, \
             patch.object(self.controller, "_load_config", return_value=({}, set())), \
             patch.object(self.controller, "_align_favorites", side_effect=[False, True]):
            table = {"S16:三国同盟": {"盐"}, "S17:试炼之路": {"盐"}}
            run = lists.put_purchase_run(99, table, table)
            context = Reader()
            argv = NS(task_detail=NS(task_id=99), custom_action_param="S16:三国同盟")
            self.assertFalse(self.controller.run(context, argv))
            self.assertIsNotNone(buy.ArbitrageBuyNeedsScan().analyze(context, argv))
            argv.custom_action_param = "S17:试炼之路"
            self.assertTrue(self.controller.run(context, argv))
            self.assertIsNotNone(buy.ArbitrageBuyScanFinished().analyze(context, argv))
            self.assertFalse(buy.ArbitrageBuyReady().run(context, argv))
            self.assertEqual(invalidate.call_count, 2)
            save.assert_called_once_with("S17:试炼之路", {"盐"})
            run["completed_day"] = DAY
            self.assertEqual(lists.completed_purchase_items(99, DAY), {("试炼之路", "盐")})


class ShopRecoveryTests(unittest.TestCase):
    def test_recovery_reuses_caller_anchor_after_success_and_failure(self):
        from unittest.mock import Mock
        self.assertIn("[JumpBack][Anchor]Replenish_ShopEntry", PIPE["Arbitrage_Replenish_EnsureShop"]["next"])
        for entry in ("Arbitrage_Merchant_Entry", "Arbitrage_Merchant_NoDiscount_Entry"):
            with self.subTest(entry=entry):
                context = Reader()
                context.set_anchor("Replenish_ShopEntry", entry)
                local = Mock()
                context.clone = lambda: local
                local.clear_hit_count.return_value = True
                local.run_task.return_value = NS(status=NS(succeeded=True),
                    nodes=[NS(name="Arbitrage_Replenish_ShopReady")])
                replenish_buy.ensure_shop(context)
                replenish_buy.ensure_shop(context)
                name, overrides = local.run_task.call_args.args
                self.assertEqual(name, "Arbitrage_Replenish_EnsureShop")
                self.assertFalse({name, "Arbitrage_Start", "Arbitrage_Merchant_Ico",
                                  "Arbitrage_Action_Hub"} & overrides.keys())
                self.assertEqual(context.get_anchor("Replenish_ShopEntry"), entry)
                local.run_task.return_value.nodes = [NS(name="Global_Null")]
                with self.assertRaisesRegex(RuntimeError, "未确认商店列表"):
                    replenish_buy.ensure_shop(context)
                self.assertEqual(context.get_anchor("Replenish_ShopEntry"), entry)

    def test_missing_caller_anchor_cannot_silently_choose_an_entry(self):
        with self.assertRaisesRegex(RuntimeError, "调用方未设置"):
            replenish_buy.ensure_shop(Reader())


class ShopAnchorLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.context = Reader()
        self.anchor = "Replenish_ShopEntry"
        self.context.set_anchor(self.anchor, "Arbitrage_Merchant_Entry")
        self.controller = result.ArbitrageSellController()
        sync = patch.object(result, "sync_from_context", return_value=True)
        sync.start()
        self.addCleanup(sync.stop)

    def argv(self, mode="sell", scope="recipes"):
        return NS(custom_action_param=json.dumps({"mode": mode, "sale_scope": scope}))

    def test_each_sale_stage_overwrites_at_start_and_clears_after_whole_run(self):
        for mode, scope in (("preview_possess", "recipes"), ("sell", "recipes"), ("sell", "materials")):
            with self.subTest(mode=mode, scope=scope):
                self.context.set_anchor(self.anchor, "Arbitrage_Merchant_Entry")

                def selling(context, params):
                    self.assertEqual(context.get_anchor(self.anchor), "Arbitrage_Merchant_NoDiscount_Entry")
                    return True

                with patch.object(self.controller, "_run", side_effect=selling):
                    self.assertTrue(self.controller.run(self.context, self.argv(mode, scope)))
                self.assertEqual(self.context.get_anchor(self.anchor), "")

    def test_market_scan_does_not_clear_callers_anchor(self):
        with patch.object(self.controller, "_run", return_value=True):
            self.assertTrue(self.controller.run(self.context, self.argv("preview_all")))
        self.assertEqual(self.context.get_anchor(self.anchor), "Arbitrage_Merchant_Entry")

    def test_failed_stopped_or_exceptional_sale_keeps_anchor(self):
        for outcome in (False, "stopped", "exception"):
            with self.subTest(outcome=outcome):
                self.context.tasker.stopping = outcome == "stopped"
                with patch.object(self.controller, "_run", return_value=outcome is not False,
                                  side_effect=RuntimeError("test") if outcome == "exception" else None):
                    if outcome == "exception":
                        with self.assertRaises(RuntimeError):
                            self.controller.run(self.context, self.argv())
                    else:
                        self.controller.run(self.context, self.argv())
                self.assertEqual(self.context.get_anchor(self.anchor), "Arbitrage_Merchant_NoDiscount_Entry")


class SaleBoundaryTests(unittest.TestCase):
    def search(self, scope, found=False):
        from unittest.mock import Mock
        end_node = PIPE[scope]["anchor"]["Arbitrage_Sell_ListEnd"]
        batch = NS(request={"item_name": "test"}, absent=Mock(), fail=Mock())
        calls = []

        def recognize(node, image):
            calls.append(node)
            hit = (node == "Agt_SellList_Ready" or node == sale._LIST and found
                   or node == "Arbitrage_Sell_Item_ListTraverse_End_Recipes")
            return NS(hit=hit)

        context = NS(get_anchor=lambda name: end_node, run_recognition=recognize,
                     get_node_object=lambda name: NS(attach={"max_pages": 1}),
                     tasker=NS(controller=NS(post_screencap=lambda: NS(wait=lambda: NS(get=lambda: NS(size=1))))))
        with patch.object(sale, "_batch", return_value=batch), patch.object(sale, "check_scope"):
            result = sale.ArbitrageSaleSearch().run(context, NS(node_name="test"))
        return result, batch, calls

    def test_recipe_stops_at_ingredients_in_both_sale_stages(self):
        for node in ("Arbitrage_PreSell_Entry", "Arbitrage_SellItem"):
            result, batch, calls = self.search(node)
            self.assertFalse(result)
            batch.absent.assert_called_once()
            batch.fail.assert_not_called()

    def test_material_stage_does_not_mark_zero_at_ingredients(self):
        result, batch, calls = self.search("Arbitrage_SellMaterials")
        self.assertFalse(result)
        batch.absent.assert_not_called()
        batch.fail.assert_called_once()

    def test_visible_target_wins_over_same_screen_category_boundary(self):
        result, batch, calls = self.search("Arbitrage_SellItem", found=True)
        self.assertTrue(result)
        batch.absent.assert_not_called()
        self.assertNotIn("Arbitrage_Sell_Item_ListTraverse_End_Recipes", calls)


if __name__ == "__main__":
    unittest.main()
