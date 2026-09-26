"""Verify price-list custom callbacks through real MaaFw, without a game."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from maa.custom_action import CustomAction
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

Library.version()  # Select local framework mode before importing AgentServer.
from maa.agent.agent_server import AgentServer

# These tests register callbacks on a local Resource, not an AgentServer process.
with patch.object(AgentServer, "_set_api_properties"), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls):
    from verify_arbitrage_sale_routing import ROOT, Shop
    from verify_arbitrage_preview import item
    from action import arbitrage_result as ar
    from action.smart_action import SmartAction


class PriceListRoutingTests(unittest.TestCase):
    def run_scan(self, pc=False, missed=0, capture_failure=False):
        if pc and not (ROOT / "assets/resource/pc/pipeline/Arbitrage.json").is_file():
            self.skipTest("PC arbitrage overlay is not present on this branch")
        with tempfile.TemporaryDirectory(prefix="mfabd2-pricelist-routing-") as temp:
            self.assertTrue(Toolkit.init_option(temp, {"logging": False}))
            self.assertTrue(Tasker.set_save_on_error(False))
            resource = Resource()
            self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
            if pc:
                self.assertTrue(resource.post_bundle(ROOT / "assets/resource/pc").wait().succeeded)
            shop = Shop()
            state = SimpleNamespace(page=0, swipes=0, calibrations=0, recos=[], scan=None)
            shop.screencap = lambda: np.full((720, 1280, 3), state.page * 100, dtype=np.uint8)

            def swipe(*args):
                state.swipes += 1
                return True

            shop.swipe = swipe
            self.assertTrue(shop.post_connection().wait().succeeded)
            tasker = Tasker()
            self.assertTrue(tasker.bind(resource, shop))

            class ObservedSmartAction(SmartAction):
                def run(self, context, argv):
                    state.recos.append(argv.reco_detail.reco_id)
                    return super().run(context, argv)

            class Probe(CustomAction):
                def run(self, context, argv):
                    if argv.node_name == "Arbitrage_Swip_Calibration_Hub":
                        state.calibrations += 1
                        if state.swipes > missed:
                            state.page = 1
                        return True
                    controller = ar.ArbitrageSellController()
                    controller._parse_current_page = lambda ctx: [
                        item("炒蘑菇" if state.page == 0 else "咖啡豆")
                    ]
                    state.scan = controller._scan_price_list(
                        context, max_scan_pages=8, stop_at_non_max=False
                    )
                    return True

            smart = ObservedSmartAction()
            self.assertTrue(resource.register_custom_action("SmartAction", smart))
            self.assertTrue(resource.register_custom_action("test_pricelist_probe", Probe()))
            fast = {"pre_delay": 0, "post_delay": 0, "rate_limit": 0}
            override = {
                "test_pricelist_scan": {**fast, "action": "Custom", "custom_action": "test_pricelist_probe"},
                "Agt_PriceList_Swip": fast,
                "Arbitrage_Swip_PriceList": {**fast, "duration": 1, "end_hold": 0},
                "Arbitrage_Swip_Calibration_Hub": {
                    **fast, "action": "Custom", "custom_action": "test_pricelist_probe", "next": []
                },
            }
            with patch("action.smart_action.time.sleep") as settle:
                if capture_failure:
                    with patch.object(smart, "_grab", return_value=None):
                        result = tasker.post_task("test_pricelist_scan", override).wait()
                else:
                    result = tasker.post_task("test_pricelist_scan", override).wait()
                self.assertTrue(result.succeeded)
                self.assertTrue(state.recos, "Native callback must enter SmartAction.run")
                self.assertTrue(all(reco > 0 for reco in state.recos))
                self.assertTrue(all(call.args == (.3,) for call in settle.call_args_list))
                self.assertEqual(settle.call_count, state.swipes)
            tasker.post_stop().wait()
            return state

    def test_callback_retries_and_returns_to_page_parser(self):
        for pc in (False, True):
            for missed in (0, 1, 2):
                with self.subTest(pc=pc, missed=missed):
                    state = self.run_scan(pc=pc, missed=missed)
                    self.assertEqual(state.swipes, missed + 1 + 3)
                    self.assertEqual(state.calibrations, state.swipes)
                    self.assertEqual([row["name"] for row in state.scan["items"]], ["炒蘑菇", "咖啡豆"])
                    self.assertTrue(state.scan["full_list_complete"])

    def test_custom_failure_is_not_swallowed_by_default_on_error(self):
        for pc in (False, True):
            with self.subTest(pc=pc):
                state = self.run_scan(pc=pc, capture_failure=True)
                self.assertEqual(state.swipes, 0)
                self.assertEqual(state.scan["termination_reason"], "swipe_failed")
                self.assertFalse(state.scan["full_list_complete"])


if __name__ == "__main__":
    unittest.main()
