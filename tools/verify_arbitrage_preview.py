"""价目表扫描与 ① 开局出售的回归检查；不连接游戏、不读写真实存档。"""

import json
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import action.arbitrage_flow as flow
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


SINCE = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def stock(quantity, minutes=5):
    """一条本轮盘点读到的库存记录（读到时刻在 SINCE 之后 minutes 分钟）。"""
    seen = SINCE.replace(minute=minutes).isoformat()
    return {"quantity": quantity, "quantity_status": "known", "quantity_observed_at": seen,
            "quantity_source": "bag_absent" if quantity == 0 else "bag_detail"}


class ArbitragePreviewTests(unittest.TestCase):
    def test_item_dispatch_matches_both_languages_from_either_input(self):
        pairs = [("三文鱼", "鮭魚"), ("黄油", "奶油"), ("香草", "草藥"),
                 ("天赋神药", "天賦特效藥")]
        for simplified, traditional in pairs:
            for source in (simplified, traditional):
                with self.subTest(source=source):
                    override = ar._sell_item_override(Context(), source)
                    self.assertEqual(override["Agt_<Sell_Item>_Ocr"]["expected"], [])
                    self.assertEqual(override["Agt_<Sell_Item>_OcrScore"]["custom_recognition_param"]["item_name"], simplified)
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
                override = ar._sell_item_override(Context(), name)
                self.assertEqual(name_i18n.name_variants(name), [name])
                self.assertEqual(override["Agt_<Sell_Item>_OcrScore"]["custom_recognition_param"]["item_name"], name)
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
                patch.object(ar, "save_market_snapshot", side_effect=lambda scan, day=None: saved.append(scan) or True):
            self.assertTrue(controller.run(context, Argv('{"mode":"preview_all"}')))
        self.assertEqual([entry["name"] for entry in saved[0]["items"]], ["烤蜂蜜苹果", "蜂蜜"])
        self.assertTrue(saved[0]["complete"])
        self.assertFalse(any(name == "Arbitrage_Sell_HUB" for name, _ in context.calls))

    def test_preview_all_keys_the_market_to_the_day_the_shop_was_entered(self):
        """店里的价目表过了 00:00 KST 不退店不换（游戏 bug）：ArbitrageMarketEnsure 带来进店那天，复用与保存都按它。"""
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: [item("烤蜂蜜苹果")]
        saved = []
        with patch.object(ar, "get_market_snapshot", return_value=None) as get, \
                patch.object(ar, "save_market_snapshot", side_effect=lambda scan, day=None: saved.append(day) or True):
            self.assertTrue(controller.run(context, Argv('{"mode":"preview_all","market_day":"2026-11-17"}')))
        get.assert_called_once_with("2026-11-17")
        self.assertEqual(saved, ["2026-11-17"])

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

    def presell(self, market, inventory, trusted=True):
        """按 ① 节点自己的参数跑出售主控；派发本身被替换，只看去了哪些柜台。"""
        context = Context()
        controller = ar.ArbitrageSellController()
        controller._parse_current_page = lambda ctx: self.fail("① 不再扫描价目表")
        params = json.dumps(PIPELINE["Arbitrage_PreSell_Run"]["custom_action_param"], ensure_ascii=False)
        since = SINCE if trusted else None
        with patch.object(ar, "get_market_snapshot", return_value=market), \
                patch.object(ar, "trust_since", return_value=since), \
                patch.object(ar, "get_inventory_items", return_value=inventory), \
                patch.object(ar, "execute_sale_item", return_value={
                    "status": "confirmed", "actual_quantity": 10, "page_ok": True}) as execute:
            completed = controller.run(context, Argv(params))
        return completed, execute, context

    def test_presell_takes_prices_from_market_and_skips_recipes_known_empty(self):
        market = {"complete": True, "items": [
            item("烤蜂蜜苹果", current_rate=118),
            item("蘑菇汤"),
            item("蜂蜜", cart="故事游戏卡12"),
            item("盐", False),
        ]}
        inventory = {"蘑菇汤": stock(0), "烤蜂蜜苹果": stock(1960)}
        completed, execute, context = self.presell(market, inventory)
        self.assertTrue(completed)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:2], (context, "烤蜂蜜苹果"))
        override = execute.call_args.args[2]
        self.assertEqual(override["Arbitrage_Sell_Item_Price_MaxCheck"]["expected"], "118%")
        self.assertEqual(execute.call_args.kwargs, {"reserve": 0})
        self.assertEqual(context.anchors["Replenish_ShopEntry"], "")

    def test_presell_visits_every_peak_recipe_when_no_record_is_trusted(self):
        market = {"complete": True, "items": [item("烤蜂蜜苹果"), item("蘑菇汤")]}
        completed, execute, _ = self.presell(market, {"蘑菇汤": stock(0)}, trusted=False)
        self.assertTrue(completed)
        self.assertEqual([call.args[1] for call in execute.call_args_list], ["烤蜂蜜苹果", "蘑菇汤"])

    def test_presell_without_peak_recipe_or_market_does_not_dispatch(self):
        completed, execute, _ = self.presell({"complete": True, "items": [item("蜂蜜")]}, {})
        self.assertTrue(completed)
        execute.assert_not_called()
        # 没有今日完整行情：① 不猜，返回失败交给节点默认退栈（跳过 ①，后续阶段照常）。
        completed, execute, _ = self.presell(None, {})
        self.assertFalse(completed)
        execute.assert_not_called()

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
        self.assertEqual(scan_modes("Arbitrage_PreSell_Entry"), {"sell"})
        self.assertEqual(scan_modes("Arbitrage_SellItem"), {"sell"})
        self.assertEqual(scan_modes("Arbitrage_SellMaterials"), {"sell"})
        self.assertFalse(PIPELINE["Arbitrage_PriceList_Open_Egress"].get("next"))
        for node in PIPELINE.values():
            self.assertNotIn("Arbitrage_Sell_Preview", node.get("anchor", {}))
            self.assertNotIn("[Anchor]Arbitrage_Sell_Preview", node.get("next", []))
        scan_node = PIPELINE["Arbitrage_GlobalMarket_Scan"]
        self.assertEqual(scan_node["custom_action_param"]["max_scan_pages"], 80)
        self.assertEqual(scan_node["next"], ["Arbitrage_PriceList_Egress"])
        # ① 与 ④ 同一套出售参数，只差日志里的阶段名；① 失败走默认退栈，不停整个商店。
        presell, final = PIPELINE["Arbitrage_PreSell_Run"], PIPELINE["Arbitrage_ShopSell_Active"]

        def sale_params(node):
            return {key: value for key, value in node["custom_action_param"].items() if key != "stage"}

        self.assertEqual(sale_params(presell), sale_params(final))
        self.assertEqual(presell["next"], final["next"])
        self.assertNotIn("on_error", presell)
        self.assertEqual(PIPELINE["Arbitrage_PreSell_Entry"]["anchor"], PIPELINE["Arbitrage_SellItem"]["anchor"])
        # 行情与出售页都准备好之后才清行情计数，③ 补买计划还要再调一次行情确认。
        self.assertEqual(PIPELINE["Arbitrage_PreSell_Entry"]["next"], [
            "[JumpBack]Arbitrage_GlobalMarket_Ensure",
            "[JumpBack]Arbitrage_Sale_EnsureShop",
            "Arbitrage_PreSell_GlobalMarket_Reset",
        ])
        reset = PIPELINE["Arbitrage_PreSell_GlobalMarket_Reset"]
        self.assertEqual(reset["custom_action_param"]["node_name"], "Arbitrage_GlobalMarket_Ensure")
        self.assertEqual(reset["next"], ["Arbitrage_PreSell_Run"])
        self.assertIn("Rec_SliderSwitch_YewOn_Clr",
                      PIPELINE["Arbitrage_PriceList_Owned_Disable"]["all_of"])
        self.assertNotIn("Arbitrage_PriceList_Owned_Enable", PIPELINE)
        self.assertNotIn("Arbitrage_PreSell_Prepare", PIPELINE)
        self.assertEqual(PIPELINE["Arbitrage_Sell_Col_Amount"]["roi"], [817, 209, 79, 344])


class PriceListSwipeTests(unittest.TestCase):
    def scan_with_swipes(self, params, missed=0, screenshot_failure=False, not_started=False):
        import numpy as np
        from action import smart_action

        context = Context()
        controller = ar.ArbitrageSellController()
        state = {"page": 0, "swipes": 0, "captures": 0, "calibrations": 0}
        events = []

        def capture():
            state["captures"] += 1
            events.append("capture")
            image = None if screenshot_failure and state["captures"] == 2 else np.full(
                (720, 1280, 3), state["page"] * 100 + state["calibrations"] * 10, dtype=np.uint8
            )
            return SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: image))

        def swipe(node, override=None):
            self.assertEqual(node, "Agt_Proxy_Blank")
            self.assertEqual(override, {node: params["proxy_override"]})
            self.assertFalse(override[node].get("next"))
            state["swipes"] += 1
            events.append("swipe")
            if state["swipes"] > missed:
                state["page"] = 1
            return Detail()

        def task(node, pipeline_override=None):
            if node != "Agt_PriceList_Swip":
                return swipe(node, pipeline_override)
            if not_started:
                return None
            argv = SimpleNamespace(node_name=node, custom_action_param=params)
            succeeded = smart_action.SmartAction().run(context, argv)
            if succeeded:
                self.assertEqual(PIPELINE[node]["next"], ["Arbitrage_Swip_Calibration_Hub"])
                state["calibrations"] += 1
                events.append("calibrate")
            return SimpleNamespace(status=SimpleNamespace(succeeded=succeeded))

        context.tasker.controller = SimpleNamespace(post_screencap=capture)
        context.run_task = task
        controller._parse_current_page = lambda ctx: [item("炒蘑菇" if state["page"] == 0 else "咖啡豆")]

        def settle(seconds):
            self.assertEqual(seconds, .3)
            events.append("settle")

        with patch.object(smart_action.time, "sleep", side_effect=settle):
            scan = controller._scan_price_list(context, max_scan_pages=8, stop_at_non_max=False)
        attempt = ["capture", "swipe", "settle", "capture"]
        expected = attempt * state["swipes"]
        if not screenshot_failure and not not_started:
            expected = attempt * (missed + 1) + ["calibrate"] + attempt * 3 + ["calibrate"]
        self.assertEqual(events, expected)
        self.assertEqual(state["calibrations"], 0 if screenshot_failure or not_started else 2)
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


class MarketEnsureDayTests(unittest.TestCase):
    """共享行情按价目表日复用；扫描跨过换日时不把它当作新一天的行情。"""

    def setUp(self):
        self.scan = SimpleNamespace(status=SimpleNamespace(succeeded=True),
                                    nodes=[SimpleNamespace(name="Arbitrage_PriceList_Egress")])
        self.overrides = []
        param = PIPELINE["Arbitrage_GlobalMarket_Scan"]["custom_action_param"]
        node = SimpleNamespace(action=SimpleNamespace(param=SimpleNamespace(custom_action_param=param)))
        local = SimpleNamespace(set_anchor=lambda *a: True, get_node_object=lambda name: node,
                                run_task=lambda name, override=None: self.overrides.append(override) or self.scan)
        self.context = SimpleNamespace(clone=lambda: local)
        self.mocks = [patch.object(flow, "sync_from_context", return_value=True),
                      patch.object(flow, "ensure_shop"),
                      patch.object(flow.mfaalog, "info"),
                      patch.object(flow.mfaalog, "error")]
        for mock in self.mocks:
            mock.start()

    def tearDown(self):
        for mock in reversed(self.mocks):
            mock.stop()

    def test_reuses_only_the_current_market_day(self):
        with patch.object(flow.store, "market_day", return_value="2026-11-08"), \
                patch.object(flow.store, "get_market_snapshot", return_value={"complete": True}) as get:
            self.assertTrue(flow.ArbitrageMarketEnsure().run(self.context, None))
        get.assert_called_once_with("2026-11-08")

    def test_scan_crossing_market_day_is_rejected(self):
        # 查缓存、进店之后、扫完各取一次价目表日：进店后才跨 0 点，这份行情记在进店那天，本段不用
        with patch.object(flow.store, "market_day", side_effect=["2026-11-07", "2026-11-07", "2026-11-08"]), \
                patch.object(flow.store, "get_market_snapshot", side_effect=[None, {"complete": True}]):
            self.assertFalse(flow.ArbitrageMarketEnsure().run(self.context, None))
        self.assertIn("换日", flow.mfaalog.error.call_args.args[0])
        scan_param = self.overrides[0]["Arbitrage_GlobalMarket_Scan"]["custom_action_param"]
        self.assertEqual(scan_param, {**PIPELINE["Arbitrage_GlobalMarket_Scan"]["custom_action_param"],
                                      "market_day": "2026-11-07"})          # 原有参数保留，只加进店那天

    def test_crossing_before_entering_the_shop_scans_the_new_day(self):
        # 在箱庭查缓存时还是旧的一天、进店时已过 0 点：店里是新表，按新的一天扫、照常使用
        with patch.object(flow.store, "market_day", side_effect=["2026-11-07", "2026-11-08", "2026-11-08"]), \
                patch.object(flow.store, "get_market_snapshot", side_effect=[None, {"complete": True}]) as get:
            self.assertTrue(flow.ArbitrageMarketEnsure().run(self.context, None))
        self.assertEqual(self.overrides[0]["Arbitrage_GlobalMarket_Scan"]["custom_action_param"]["market_day"], "2026-11-08")
        self.assertEqual(get.call_args.args, ("2026-11-08",))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
