"""启动变现预览模式回归检查；不连接游戏、不读写真实存档。"""

import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import action.arbitrage_result as ar
from utils import name_i18n


PIPELINE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))


class Detail:
    status = SimpleNamespace(succeeded=True)


class Context:
    def __init__(self):
        self.tasker = SimpleNamespace(stopping=False)
        self.calls = []
        self.anchors = {}

    def set_anchor(self, name, target):
        self.anchors[name] = target
        return True

    def get_node_object(self, name):
        if name == "Arbitrage_ShopSell_Active":
            return SimpleNamespace(attach={"default": "烤蜂蜜苹果"})
        return SimpleNamespace(attach={})

    def get_node_data(self, name):
        return dict(PIPELINE[name])

    def run_task(self, node, pipeline_override=None):
        self.calls.append((node, pipeline_override))
        return Detail()


class Argv:
    def __init__(self, params=""):
        self.custom_action_param = params


def item(name, is_max=True, cart="剧情游戏卡17", current_rate=120):
    return {
        "name": name,
        "is_max_price": is_max,
        "target_cartridge": cart,
        "cart_score": 1.0,
        "cart_conflict": False,
        "alt_cartridge": "",
        "current_rate": current_rate,
    }


class ArbitragePreviewTests(unittest.TestCase):
    def test_item_dispatch_matches_both_languages_from_either_input(self):
        pairs = [("三文鱼", "鮭魚"), ("黄油", "奶油"), ("香草", "草藥"),
                 ("天赋神药", "天賦特效藥")]
        for simplified, traditional in pairs:
            for source in (simplified, traditional):
                with self.subTest(source=source):
                    override = ar._sell_item_override(Context(), source)
                    patterns = override["Agt_<Sell_Item>_Ocr"]["expected"]
                    for target in (simplified, traditional):
                        self.assertTrue(any(re.search(pattern, target) for pattern in patterns))
                    for target in ("其他物品", simplified + "汤", "大" + traditional):
                        self.assertFalse(any(re.search(pattern, target) for pattern in patterns))
                    self.assertEqual(name_i18n.canon(source), simplified)

    def test_item_variants_cover_dictionary_without_changing_identity(self):
        for traditional, simplified in name_i18n._load().items():
            variants = name_i18n.name_variants(simplified)
            self.assertIn(traditional, variants)
            self.assertEqual(len(variants), len(set(variants)))
            self.assertEqual({name_i18n.canon(name) for name in variants}, {simplified})

    def test_unknown_item_and_missing_dictionary_preserve_literal_name(self):
        for dictionary in (None, {}):
            with patch.object(name_i18n, "_NORM", dictionary):
                name = "未知物品(甲)+."
                patterns = ar._sell_item_override(Context(), name)["Agt_<Sell_Item>_Ocr"]["expected"]
                self.assertEqual(name_i18n.name_variants(name), [name])
                self.assertTrue(any(re.search(pattern, name) for pattern in patterns))
                self.assertFalse(any(re.search(pattern, "未知物品甲甲X") for pattern in patterns))
        with patch.object(name_i18n, "_NORM", {}):
            self.assertEqual(name_i18n.name_variants("鮭魚"), ["鮭魚"])

    def setUp(self):
        ar._RECIPE_NAMES = None
        self.sync = patch.object(ar, "sync_from_context", return_value=True)
        self.sync.start()

    def tearDown(self):
        self.sync.stop()

    def test_preview_all_saves_and_never_dispatches_sell(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: [item("烤蜂蜜苹果"), item("蜂蜜", False)]
        saved = []
        with patch.object(ar, "get_market_snapshot", return_value=None), \
                patch.object(ar, "save_market_snapshot", side_effect=lambda scan: saved.append(scan) or True):
            self.assertTrue(controller.run(context, Argv('{"mode":"preview_all"}')))
        self.assertEqual([entry["name"] for entry in saved[0]["items"]], ["烤蜂蜜苹果", "蜂蜜"])
        self.assertTrue(saved[0]["complete"])
        self.assertFalse(any(name == "Arbitrage_Sell_HUB" for name, _ in context.calls))

    def test_preview_all_reuses_complete_daily_cache(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: self.fail("命中缓存后不应再解析页面")
        cached = {"complete": True, "items": [{"name": "烤蜂蜜苹果", "is_max_price": True}]}
        with patch.object(ar, "get_market_snapshot", return_value=cached), \
                patch.object(ar, "save_market_snapshot") as save:
            self.assertTrue(controller.run(context, Argv('{"mode":"preview_all"}')))
        save.assert_not_called()
        self.assertEqual(context.calls, [])

    def test_preview_possess_records_all_peak_items_but_sells_only_recipe(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: [
            {**item("烤蜂蜜苹果", current_rate=118), "raw_name": "烤蜂蜜苹杲", "name_confirmed": True},
            item("蜂蜜", cart="故事游戏卡12", current_rate=118),
            item("盐", False, current_rate=118),
        ]
        saved = []
        market = {"complete": True, "items": [
            item("烤蜂蜜苹果", current_rate=118),
        ]}
        with patch.object(ar, "get_market_snapshot", return_value=market), \
                patch.object(ar, "save_possession_snapshot", side_effect=lambda scan: saved.append(scan) or True), \
                patch.object(ar, "execute_sale_item", return_value={
                    "status": "confirmed", "actual_quantity": 10, "page_ok": True}) as execute:
            self.assertTrue(controller.run(context, Argv('{"mode":"preview_possess"}')))
        self.assertEqual([entry["name"] for entry in saved[0]["items"]], ["烤蜂蜜苹果", "蜂蜜", "盐"])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:2], (context, "烤蜂蜜苹果"))
        override = execute.call_args.args[2]
        patterns = override["Agt_<Sell_Item>_Ocr"]["expected"]
        for name in ("烤蜂蜜苹果", "烤蜂蜜蘋果"):
            self.assertTrue(any(re.fullmatch(pattern, name) for pattern in patterns))
        self.assertEqual(override["Arbitrage_Sell_Item_Price_MaxCheck"]["expected"], "118%")
        self.assertEqual(execute.call_args.kwargs, {"reserve": 0})

    def test_possession_plan_uses_lowest_peak_recipe_rate(self):
        recipes = {"蘑菇汤", "街头烤鸡肉串"}
        snapshot = {
            "complete": True,
            "items": [
                item("蘑菇汤", current_rate=118),
                item("街头烤鸡肉串", current_rate=120),
                item("胡椒", current_rate=117),
                item("酱炒牛排", False, current_rate=120),
            ],
        }
        plan = ar._possession_scan_plan(snapshot, recipes)
        self.assertTrue(plan["usable"])
        self.assertFalse(plan["skip"])
        self.assertEqual(plan["target_rate_floor"], 118)
        self.assertEqual(plan["target_names"], ["蘑菇汤", "街头烤鸡肉串"])
        snapshot["items"][0]["current_rate"] = None
        self.assertFalse(ar._possession_scan_plan(snapshot, recipes)["usable"])

    def test_possession_scan_stops_after_ordered_page_crosses_dynamic_boundary(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: [
            item("炒蘑菇", current_rate=120),
            item("咖啡豆", current_rate=120),
            item("鱼子酱罐头", False, current_rate=118),
        ]
        scan = controller._scan_price_list(
            context, max_scan_pages=80, stop_at_non_max=False, stop_below_rate=120
        )
        self.assertEqual(scan["termination_reason"], "target_rate_boundary")
        self.assertTrue(scan["sale_candidates_complete"])
        self.assertFalse(scan["full_list_complete"])
        self.assertEqual(scan["pages_scanned"], 1)
        self.assertEqual(scan["lowest_observed_rate"], 118)
        self.assertEqual(context.calls, [])

    def test_unreadable_rate_disables_early_stop_and_scans_to_bottom(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        page = [
            item("炒蘑菇", current_rate=120),
            item("咖啡豆", current_rate=None),
            item("鱼子酱罐头", False, current_rate=118),
        ]
        controller._parse_current_page = lambda ctx: page
        scan = controller._scan_price_list(
            context, max_scan_pages=80, stop_at_non_max=False, stop_below_rate=120
        )
        self.assertEqual(scan["termination_reason"], "repeated_page")
        self.assertTrue(scan["full_list_complete"])
        self.assertFalse(scan["rate_order_safe"])
        self.assertEqual([name for name, _ in context.calls], ["Agt_PriceList_Swip"])

    def test_no_peak_recipe_skips_possession_scan(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: self.fail("没有峰值料理时不应扫描已有物")
        market = {"complete": True, "items": [item("胡椒", current_rate=120)]}
        saved = []
        with patch.object(ar, "get_market_snapshot", return_value=market), \
                patch.object(ar, "save_possession_snapshot",
                             side_effect=lambda scan: saved.append(scan) or True):
            self.assertTrue(controller.run(context, Argv('{"mode":"preview_possess"}')))
        self.assertEqual(saved[0]["termination_reason"], "no_peak_recipe")
        self.assertTrue(saved[0]["sale_candidates_complete"])
        self.assertEqual(saved[0]["items"], [])
        self.assertEqual(context.calls, [])

    def test_invalid_mode_fails_closed(self):
        context = Context()
        controller = ar.ArbitrageSellController()
        self.assertFalse(controller.run(context, Argv('{"mode":"preview_typo"}')))
        self.assertEqual(context.calls, [])

    def test_money_parser_handles_separators_and_stuck_percentage(self):
        self.assertEqual(ar._money_token_value("4,416"), 4416)
        self.assertEqual(ar._money_token_value("4.416120%"), 4416)
        self.assertEqual(ar._money_token_value("110"), 110)
        self.assertIsNone(ar._money_token_value("120%"))
        self.assertIsNone(ar._money_token_value("当前"))
        self.assertEqual(ar._max_price_verdict({4}, {4}, {"118"}, {"120"}), (True, "amount"))
        self.assertEqual(ar._max_price_verdict({110}, {120}, {"120"}, {"120"}), (False, "amount"))
        self.assertEqual(ar._max_price_verdict(set(), set(), {"120"}, {"120"}),
                         (True, "rate_fallback"))

    def test_pipeline_scan_callers_keep_separate_modes(self):
        def scan_modes(entry):
            pending, visited, modes = [entry], set(), set()
            while pending:
                name = pending.pop()
                if name in visited or name not in PIPELINE:
                    continue
                visited.add(name)
                node = PIPELINE[name]
                if node.get("custom_action") == "ArbitrageSellController":
                    modes.add(node["custom_action_param"]["mode"])
                    continue
                pending.extend(target.removeprefix("[JumpBack]")
                               for target in node.get("next", []))
            return modes

        self.assertEqual(scan_modes("Arbitrage_GlobalMarket_Entry"), {"preview_all"})
        self.assertEqual(scan_modes("Arbitrage_PreSell_GlobalMarket_Reset"), {"preview_possess"})
        self.assertEqual(scan_modes("Arbitrage_SellItem"), {"sell"})
        self.assertEqual(scan_modes("Arbitrage_SellMaterials"), {"sell"})
        self.assertFalse(PIPELINE["Arbitrage_PriceList_Open_Egress"].get("next"))
        for node in PIPELINE.values():
            self.assertNotIn("Arbitrage_Sell_Preview", node.get("anchor", {}))
            self.assertNotIn("[Anchor]Arbitrage_Sell_Preview", node.get("next", []))
        for name in ("Arbitrage_GlobalMarket_Scan", "Arbitrage_PreSell_Run"):
            node = PIPELINE[name]
            self.assertEqual(node["custom_action_param"]["max_scan_pages"], 80)
            self.assertEqual(node["next"], ["Arbitrage_PriceList_Egress"])
        self.assertIn("Rec_SliderSwitch_YewOn_Clr",
                      PIPELINE["Arbitrage_PriceList_Owned_Disable"]["all_of"])
        self.assertIn("Rec_SliderSwitch_GryOff_Clr",
                      PIPELINE["Arbitrage_PriceList_Owned_Enable"]["all_of"])
        self.assertEqual(PIPELINE["Arbitrage_Sell_Col_Amount"]["roi"], [817, 209, 79, 344])


class PriceListSwipeTests(unittest.TestCase):
    def scan_with_swipes(self, params, missed=0, screenshot_failure=False, not_started=False):
        import numpy as np
        from action import smart_action

        context = Context()
        controller = ar.ArbitrageSellController()
        state = {"page": 0, "swipes": 0, "captures": 0}
        events = []

        def capture():
            state["captures"] += 1
            events.append("capture")
            image = None if screenshot_failure and state["captures"] == 2 else np.full(
                (720, 1280, 3), state["page"] * 100, dtype=np.uint8
            )
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

        def swipe(node, override=None):
            self.assertEqual(node, "Arbitrage_Swip_PriceList")
            self.assertEqual(PIPELINE[node]["next"], ["Arbitrage_Swip_Calibration_Hub"])
            self.assertIsNone(override)
            state["swipes"] += 1
            events.append("swipe_and_calibrate")
            if state["swipes"] > missed:
                state["page"] = 1
            return Detail()

        def task(node, pipeline_override=None):
            if node != "Agt_PriceList_Swip":
                return swipe(node, pipeline_override)
            if not_started:
                return None
            argv = SimpleNamespace(node_name=node, custom_action_param=params)
            return SimpleNamespace(status=SimpleNamespace(
                succeeded=smart_action.SmartAction().run(context, argv)
            ))

        context.tasker.controller = SimpleNamespace(post_screencap=capture)
        context.run_task = task
        controller._parse_current_page = lambda ctx: [item("炒蘑菇" if state["page"] == 0 else "咖啡豆")]

        def settle(seconds):
            self.assertEqual(seconds, .3)
            events.append("settle")

        with patch.object(smart_action.time, "sleep", side_effect=settle):
            scan = controller._scan_price_list(context, max_scan_pages=8, stop_at_non_max=False)
        self.assertEqual(events, ["capture", "swipe_and_calibrate", "settle", "capture"] * state["swipes"])
        return scan, state["swipes"]

    def test_transient_misses_retry_without_skipping_pages_and_bottom_is_bounded(self):
        resources = [PIPELINE]
        pc_path = ROOT / "assets/resource/pc/pipeline/Arbitrage.json"
        if pc_path.is_file():
            resources.append(json.loads(pc_path.read_text(encoding="utf-8")))
        for resource in resources:
            params = resource["Agt_PriceList_Swip"]["custom_action_param"]
            for missed in (0, 1, 2):
                with self.subTest(roi=params["detect_roi"], missed=missed):
                    scan, count = self.scan_with_swipes(params, missed=missed)
                    self.assertEqual([row["name"] for row in scan["items"]], ["炒蘑菇", "咖啡豆"])
                    self.assertEqual(count, missed + 1 + 3)
                    self.assertEqual(scan["termination_reason"], "repeated_page")
                    self.assertTrue(scan["full_list_complete"])

    def test_screenshot_failure_does_not_confirm_bottom(self):
        params = PIPELINE["Agt_PriceList_Swip"]["custom_action_param"]
        scan, count = self.scan_with_swipes(params, screenshot_failure=True)
        self.assertEqual(count, 1)
        self.assertEqual(scan["termination_reason"], "swipe_failed")
        self.assertFalse(scan["complete"])
        self.assertFalse(scan["full_list_complete"])

    def test_missing_action_does_not_confirm_bottom(self):
        params = PIPELINE["Agt_PriceList_Swip"]["custom_action_param"]
        scan, count = self.scan_with_swipes(params, not_started=True)
        self.assertEqual(count, 0)
        self.assertEqual(scan["termination_reason"], "swipe_not_started")
        self.assertFalse(scan["full_list_complete"])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
