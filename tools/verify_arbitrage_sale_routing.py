"""Exercise real Maa routing with an in-memory shop; never connect to a game.

Run: python -B tools/verify_arbitrage_sale_routing.py -v
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from maa.controller import CustomController
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer

with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls):
    from action import arbitrage_sell_batch as sale, arbitrage_buy_precise as buy
    from action.clear_node_hit_count import ClearNodeHitCountAction

PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
RETURN_ANCHOR = "SellList_Return"
SELECT_ANCHOR = "PackSelect_Next"
RECOVERY = "Arbitrage_Sell_Item_ListReset_Recovery"
RETURN = "Arbitrage_Sell_Item_ListReset_Return"
SEARCH = "Arbitrage_Sell_Item_Search"
SWIPE = "Arbitrage_ItemList_Swip"
FAILED = "Arbitrage_Sell_Item_BatchExitFailed"
END = "Arbitrage_Sell_Item_BatchEnd"
BUY_ENTRY = "Arbitrage_PreciseBuy_PackEntry"
REQUEST = {"request_id": "offline", "item_name": "甜辣酱", "cartridge": "剧情游戏卡10",
           "reserve": 0, "target": 1, "dry_run": True}


class Shop(CustomController):
    def __init__(self):
        super().__init__()
        self.tab = "sell"
        self.top = False
        self.reset_works = True
        self.sort_moves = False
        self.fail_search = False
        self.internal_swipes = 0
        self.swipes = 0
        self.tab_changes = []
        self.searched = []
        self.routes = []

    def connect(self): return True
    def request_uuid(self): return "sale-routing-offline"
    def get_features(self): return 0
    def screencap(self): return np.zeros((720, 1280, 3), dtype=np.uint8)
    def start_app(self, *args): return False
    def stop_app(self, *args): return False
    def touch_down(self, *args): return False
    def touch_move(self, *args): return False
    def touch_up(self, *args): return False
    def click_key(self, *args): return False
    def input_text(self, *args): return False
    def key_down(self, *args): return False
    def key_up(self, *args): return False
    def swipe(self, *args): return False

    def click(self, x, y):
        if x > 200:
            # Clicking the already-selected cartridge does not reset the item list.
            return True
        new_tab = "buy" if y < 150 else "sell"
        if new_tab != self.tab:
            self.tab_changes.append(new_tab)
            if new_tab == "sell" and self.reset_works:
                self.top = True
        self.tab = new_tab
        return True


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mfabd2-sale-routing-")
        self.addCleanup(self.temp.cleanup)
        self.assertTrue(Toolkit.init_option(self.temp.name, {"logging": False}))
        self.assertTrue(Tasker.set_save_on_error(False))
        self.resource = Resource()
        self.assertTrue(self.resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
        self.shop = Shop()
        self.assertTrue(self.shop.post_connection().wait().succeeded)
        self.tasker = Tasker()
        self.assertTrue(self.tasker.bind(self.resource, self.shop))
        shop = self.shop
        self.callback_errors = []
        errors = self.callback_errors

        class Recognize(CustomRecognition):
            def analyze(self, context, argv):
                kind = json.loads(argv.custom_recognition_param)["kind"]
                box = (300, 270, 20, 20)
                hit = True
                if kind == "buy_label": box = (100, 100, 20, 20)
                elif kind == "sell_label": box = (100, 200, 20, 20)
                elif kind == "buy_ready": hit = shop.tab == "buy"
                elif kind == "sell_ready": hit = shop.tab == "sell"
                elif kind == "top": hit = shop.top and shop.tab == "sell"
                elif kind == "never": hit = False
                if hit:
                    return CustomRecognition.AnalyzeResult(box=box, detail={"kind": kind})
                return None

        class Work(CustomAction):
            def run(self, context, argv):
                if argv.node_name == SEARCH:
                    # The shared reset must return before any search is allowed.
                    if not (shop.top and shop.tab == "sell" and not context.get_anchor(RETURN_ANCHOR)):
                        errors.append("search reached without a ready list and normal return route")
                        return False
                    shop.searched.append(argv.node_name)
                    for _ in range(shop.internal_swipes):
                        moved = context.run_task(SWIPE)
                        if moved is None or not moved.status.succeeded:
                            errors.append("Python-owned swipe did not finish")
                            return False
                    return not shop.fail_search
                elif argv.node_name == SWIPE:
                    shop.swipes += 1
                elif argv.node_name == "Arbitrage_ItemList_Sorting_Entry" and shop.sort_moves:
                    shop.top = False
                return True

        self.assertTrue(self.resource.register_custom_recognition("test_route_reco", Recognize()))
        self.assertTrue(self.resource.register_custom_action("test_route_work", Work()))
        self.assertTrue(self.resource.register_custom_action("ClearNodeHitCount", ClearNodeHitCountAction()))
        # Replace only visual evidence and business callbacks. Production flow,
        # anchors, hit counts and error handling are executed by the real engine.
        self.fixture = {
            name: {"pre_delay": 0, "post_delay": 0, "rate_limit": 0, "timeout": 20,
                   "pre_wait_freezes": 0, "post_wait_freezes": 0}
            for name in PIPE
        }
        for name, kind in {
            "Arbitrage_Buy_Button_Chg": "buy_label",
            "Arbitrage_Buy_Button": "buy_ready",
            "Agt_SellList_BuyReady": "buy_ready",
            "Arbitrage_Sell_Item_ListReset_Sell_Chg": "sell_label",
            "Arbitrage_Sell_Type_Clr": "sell_ready",
            "Arbitrage_PackList_ResetEnter": "always",
            "Arbitrage_Sell_PackShopSwich": "always",
            "Arbitrage_Sell_PackShopSwich_Clr": "always",
            "Arbitrage_Sell_Item_ListReset_PostOcr": "top",
            "Arbitrage_Merchant_Egress": "always",
            "Arbitrage_Sell_Item_SellMenu": "never",
            "Arbitrage_Sell_Item_ListTraverse": "never",
        }.items():
            self.fixture[name].update(recognition="Custom", custom_recognition="test_route_reco",
                                      custom_recognition_param={"kind": kind})
        for name in (SEARCH, "Arbitrage_Sell_Item_AmountMismatch", "Arbitrage_ItemList_Sorting_Entry"):
            self.fixture[name].update(action="Custom", custom_action="test_route_work")
        self.fixture["Arbitrage_ItemList_Sorting_Entry"].update(recognition="DirectHit", next=[])
        self.fixture[SWIPE].update(recognition="DirectHit", action="Custom", custom_action="test_route_work",
                                   next=[])
        # End at the search boundary; quantity selection and payment are outside this test.
        self.fixture[SEARCH]["next"] = ["Arbitrage_Sell_Item_BatchExit"]

    def dispatch(self, entries):
        owner = self

        class Dispatch(CustomAction):
            def run(self, context, argv):
                for entry, mode in entries:
                    local = context.clone()
                    local.set_anchor(RETURN_ANCHOR, RETURN)
                    owner.shop.top = owner.initial_top
                    owner.shop.tab = mode
                    cfg = buy.buy_overrides(local, REQUEST) if mode == "buy" else sale._overrides({}, REQUEST)
                    detail = local.run_task(entry, cfg)
                    if detail is None:
                        owner.callback_errors.append("missing task detail")
                        return False
                    owner.shop.routes.append([node.name for node in detail.nodes])
                    if local.get_anchor(RETURN_ANCHOR):
                        owner.callback_errors.append("reset return anchors leaked after completion")
                    if END in owner.shop.routes[-1] or "Arbitrage_Sell_End" in owner.shop.routes[-1]:
                        if local.get_anchor(SELECT_ANCHOR):
                            owner.callback_errors.append("shop route leaked after completion")
                return True

        self.assertTrue(self.resource.register_custom_action("test_route_dispatch", Dispatch()))
        self.fixture["test_route_dispatch"] = {"action": "Custom", "custom_action": "test_route_dispatch"}
        self.assertTrue(self.resource.override_pipeline(self.fixture))
        job = self.tasker.post_task("test_route_dispatch")
        self.assertTrue(job.wait().succeeded)
        self.assertEqual(self.callback_errors, [])
        self.assertEqual(len(self.shop.routes), len(entries))
        self.assertEqual(self.shop.swipes, self.shop.internal_swipes * len(self.shop.searched),
                         "the visual swipe edge must not issue additional swipes")
        return self.shop.routes[-1]

    def test_same_card_bottom_recovers_once_before_search(self):
        self.initial_top = False
        names = self.dispatch([("Arbitrage_Sell_HUB", "sell")])
        self.assertEqual(names.count(RECOVERY), 1)
        self.assertLess(names.index(RETURN), names.index(SEARCH))
        self.assertEqual(self.shop.tab_changes, ["buy", "sell"])
        self.assertIn(END, names)

    def test_top_needs_no_tab_reset(self):
        self.initial_top = True
        names = self.dispatch([("Arbitrage_Sell_HUB", "sell")])
        self.assertNotIn(RECOVERY, names)
        self.assertEqual(self.shop.tab_changes, [])
        self.assertIn(SEARCH, names)

    def test_failed_search_does_not_fall_through_to_visual_swipe_edge(self):
        self.initial_top = True
        self.shop.fail_search = True
        names = self.dispatch([("Arbitrage_Sell_HUB", "sell")])
        self.assertIn("Arbitrage_Sell_Item_BatchExit", names)
        self.assertNotIn(SWIPE, names)
        self.assertEqual(self.shop.swipes, 0)

    def test_only_python_owned_swipes_execute(self):
        self.initial_top = True
        self.shop.internal_swipes = 1
        names = self.dispatch([("Arbitrage_Sell_HUB", "sell")])
        self.assertNotIn(SWIPE, names)
        self.assertEqual(self.shop.swipes, 1)

    def test_sorting_can_require_normal_reset(self):
        self.initial_top = True
        self.shop.sort_moves = True
        names = self.dispatch([("Arbitrage_Sell_HUB", "sell")])
        self.assertNotIn(RECOVERY, names)
        self.assertEqual(self.shop.tab_changes, ["buy", "sell"])
        self.assertIn(SEARCH, names)

    def test_failed_recovery_does_not_search_or_report_batch_end(self):
        self.initial_top = False
        self.shop.reset_works = False
        names = self.dispatch([("Arbitrage_Sell_HUB", "sell")])
        self.assertEqual(names.count(RECOVERY), 1)
        self.assertIn(FAILED, names)
        self.assertNotIn(SEARCH, names)
        self.assertNotIn(END, names)
        self.assertEqual(self.shop.tab_changes, ["buy", "sell"])

    def test_resume_and_mismatch_replace_stale_reset_routes(self):
        self.initial_top = False
        self.dispatch([("Arbitrage_Sell_Item_Resume", "sell"),
                       ("Arbitrage_Sell_Item_AmountMismatch", "sell")])
        for names in self.shop.routes:
            self.assertIn(SEARCH, names)
            self.assertIn(END, names)
            self.assertNotIn(RETURN, names)

    def test_normal_reset_failure_keeps_its_batch_exit(self):
        self.initial_top = False
        self.shop.reset_works = False
        names = self.dispatch([("Arbitrage_Sell_Item_Resume", "sell")])
        self.assertNotIn(SEARCH, names)
        self.assertNotIn(RETURN, names)
        self.assertIn("Arbitrage_Sell_Item_BatchExit", names)
        self.assertIn(END, names)

    def test_new_item_in_same_task_gets_its_own_recovery_attempt(self):
        self.initial_top = False
        self.dispatch([("Arbitrage_Sell_HUB", "sell")] * 2)
        for names in self.shop.routes:
            self.assertEqual(names.count(RECOVERY), 1)
            self.assertIn(SEARCH, names)
        self.assertEqual(self.shop.tab_changes, ["buy", "sell"] * 2)

    def test_buy_then_sell_use_separate_selected_routes(self):
        self.initial_top = False
        self.dispatch([(BUY_ENTRY, "buy"), ("Arbitrage_Sell_HUB", "sell")])
        buying, selling = self.shop.routes
        self.assertIn("Arbitrage_PreciseBuy_Search", buying)
        self.assertIn("Arbitrage_PreciseBuy_NotFound", buying)
        self.assertIn("Arbitrage_Sell_End", buying)
        self.assertNotIn("Arbitrage_Sell_PackShopSwich_PostOcr", buying)
        self.assertNotIn(RECOVERY, buying)
        self.assertIn(SEARCH, selling)
        self.assertEqual(selling.count(RECOVERY), 1)


if __name__ == "__main__":
    unittest.main()
