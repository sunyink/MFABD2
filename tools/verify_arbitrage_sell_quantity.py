"""出售堆叠上限与跨批留存回归；模拟数量控件及金币确认，不连接游戏或写存档。

运行：python -B tools/verify_arbitrage_sell_quantity.py -v
读数由内存界面提供，本脚本不验证真实 OCR 或按钮坐标。
"""

import json
import re
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from action import arbitrage_sell_batch as dispatch
from action import arbitrage_sell_quantity as quantity
from action import arbitrage_buy_precise as buy


class QuantityUI(quantity.SaleQuantityAdjuster):
    """独立保存总库存、当前堆叠与选量，供生产算法操作。"""

    def __init__(self, inventory, stack, *, selected=1, reserve=0, target=None, request=None):
        context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        config = {"timeout_seconds": 90, "max_adjustments": 80, "read_attempts": 3,
                  "read_interval_ms": 0, "slider_range": [325, 589]}
        super().__init__(context, config, request or {
            "item_name": "香草", "reserve": reserve, "target": target})
        self.inventory = inventory
        self.stack = stack
        self.selected = selected
        self.actions = []
        self.ineffective = set()
        self.change_inventory = False

    def read(self, full=False):
        self.check_running()
        return (self.inventory, self.selected) if full else self.selected

    def observe_sale(self, inventory_only=False):
        return self.inventory if inventory_only else (self.inventory, self.selected, self.selected * 33)

    def action(self, key, x=None):
        self.check_running()
        if self.adjustments >= self.max_adjustments:
            raise RuntimeError("模拟控件达到操作上限")
        self.adjustments += 1
        self.actions.append(key)
        if key not in self.ineffective:
            if key == "max_node":
                self.selected = self.stack
            elif key == "min_node":
                self.selected = 1
            elif key == "slider_node":
                fraction = (x - 325) / 264
                self.selected = 1 + round((self.stack - 1) * fraction ** 1.2)
            else:
                direction, step, _ = key.split("_")
                delta = (1 if step == "one" else 10) * (1 if direction == "plus" else -1)
                self.selected = max(1, min(self.stack, self.selected + delta))
        if self.change_inventory:
            self.inventory -= 1
            self.change_inventory = False


class StackQuantityTests(unittest.TestCase):
    def test_purchase_item_dispatch_matches_simplified_and_traditional(self):
        pipeline = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        context = SimpleNamespace(get_node_object=lambda name: SimpleNamespace(attach=pipeline[name].get("attach", {})))
        for name in ("三文鱼", "鮭魚"):
            override = buy.buy_overrides(context, {"item_name": name, "cartridge": "剧情游戏卡1"})
            self.assertEqual(override["Agt_<Sell_Item>_Ocr"]["expected"], [])
            self.assertEqual(override["Agt_<Sell_Item>_OcrScore"]["custom_recognition_param"]["item_name"], "三文鱼")

    def test_purchase_shop_patterns_follow_loaded_resources(self):
        base = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        pc_path = ROOT / "assets/resource/pc/pipeline/Arbitrage.json"
        overlays = [{}]
        if pc_path.exists():
            overlays.append(json.loads(pc_path.read_text(encoding="utf-8")))
        shops = json.loads((ROOT / "agent/data/arbitrage_replenish.json").read_text(encoding="utf-8"))["shops"]
        catalog = buy.load_purchase_catalog()
        selectors = {key.split(":", 1)[1]: value["selector"] for key, value in catalog.items()}
        for overlay in overlays:
            for shop in shops:
                selector = selectors[shop]
                node = {**base[selector], **overlay.get(selector, {})}
                data = {"enabled": False, "max_hit": 0,
                        "recognition": {"type": "OCR", "param": {"expected": node["expected"]}}}
                context = SimpleNamespace(
                    get_node_data=lambda name: data if name == selector else None,
                    get_node_object=lambda name: SimpleNamespace(attach=base[name].get("attach", {})))
                with self.subTest(shop=shop, pc=bool(overlay)):
                    override = buy.buy_overrides(context, {"item_name": "蘑菇", "shop_name": shop})
                    self.assertEqual(override["Arbitrage_Sell_PackShopSwich"]["expected"], node["expected"])

    def test_purchase_shop_patterns_preserve_runtime_regex_and_partial_names(self):
        expected = ["国同盟", "國同盟", "[魯鲁]的迷[宮宫]", "新资源名称"]
        context = SimpleNamespace(get_node_data=lambda name: {
            "recognition": {"type": "OCR", "param": {"expected": expected}}})
        actual = buy._shop_expected(context, "三国同盟")
        for text in ("三国同盟", "三國同盟", "魯的迷宮", "新资源名称"):
            self.assertTrue(any(re.search(pattern, text) for pattern in actual))
        self.assertEqual(actual, expected)
        self.assertIsNot(actual, expected)

    def test_purchase_shop_patterns_reject_missing_or_invalid_configuration(self):
        for node in (None, {}, {"recognition": {"type": "TemplateMatch"}},
                     *({"recognition": {"type": "OCR", "param": {"expected": expected}}}
                       for expected in (None, [], "血骑士", [""], [" "], [123]))):
            with self.subTest(node=node):
                context = SimpleNamespace(get_node_data=lambda name: node)
                with self.assertRaises(ValueError):
                    buy._shop_expected(context, "血骑士")
        with self.assertRaises(ValueError):
            buy._shop_expected(context, "未收录柜台")

    def test_purchase_recognition_uses_resources_without_overwriting_sale_quote(self):
        pipeline = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        context = SimpleNamespace(get_node_object=lambda name: SimpleNamespace(attach=pipeline[name].get("attach", {})))
        override = buy.buy_overrides(context, {"item_name": "蘑菇", "cartridge": "剧情游戏卡1"})
        config = override["Arbitrage_Sell_Item_Quantity"]["attach"]
        self.assertNotIn("Arbitrage_Sell_Item_Price_MaxCheck", override)
        self.assertEqual(config["price_node"], "Agt_BuyConfirm_Ocr")
        self.assertEqual(override["Arbitrage_Sell_Type_Clr"],
                         {"recognition": "Or", "any_of": ["Agt_SellList_BuyReady"]})
        for name in ("Rec_<Arbitrage_Sell_Item_SellMenu>_Ocr_01", "Arbitrage_Sell_Item_Selling"):
            self.assertEqual(override[name]["any_of"], [config["price_node"]])
            self.assertNotIn("roi", override[name])
            self.assertNotIn("expected", override[name])

    def test_purchase_button_and_available_count_match_separate_lines(self):
        pipeline = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        button = pipeline["Agt_BuyConfirm_Ocr"]["expected"]
        available = pipeline["Agt_BuyQuantity_Available_Ocr"]["expected"]
        for label, count in (("购买", "可购买400个"), ("購買", "可購買400個")):
            with self.subTest(label=label):
                self.assertTrue(any(re.fullmatch(pattern, label) for pattern in button))
                self.assertFalse(any(re.fullmatch(pattern, count) for pattern in button))
                self.assertTrue(any(re.fullmatch(pattern, count) for pattern in available))
                self.assertFalse(any(re.fullmatch(pattern, label) for pattern in available))
                self.assertEqual(buy.parse_available([count]), 400)

    def test_inventory_labels_from_simplified_and_pc_screens(self):
        for text in ("拥有153,730个", "持有153,730個", "持有１５３，７３０個"):
            with self.subTest(text=text):
                self.assertEqual(quantity.parse_quantity([text], inventory=True), 153730)

    def test_inventory_requires_a_complete_unambiguous_label(self):
        for texts in (["持有153.7"], ["持有153,730"], ["可購買400個"],
                      ["持有153,730個", "持有153,731個"]):
            with self.subTest(texts=texts):
                with self.assertRaises(ValueError):
                    quantity.parse_quantity(texts, inventory=True)

    def test_real_run_stack_maxima_are_accepted(self):
        for inventory, maximum in [(329717, 29720), (171120, 71121), (534611, 97974)]:
            with self.subTest(inventory=inventory):
                ui = QuantityUI(inventory, maximum)
                ready = ui.prepare()
                self.assertEqual(ready["selected"], maximum)
                self.assertEqual(ready["maximum"], maximum)
                self.assertEqual(ready["inventory"], inventory)
                self.assertEqual(ready["quoted_total"], maximum * 33)
                self.assertEqual(ui.actions, ["max_node"])

    def test_reserve_is_deducted_from_total_inventory(self):
        # 当前叠71121全卖仍留99999，不能先从当前叠扣一次保留量。
        self.assertEqual(QuantityUI(171120, 71121, reserve=8400).prepare()["selected"], 71121)

    def test_reserved_tail_is_adjusted_within_current_stack(self):
        ui = QuantityUI(171120, 71121, reserve=121120)
        self.assertEqual(ui.prepare()["selected"], 50000)
        self.assertIn("slider_node", ui.actions)

    def test_explicit_remaining_target_caps_current_stack(self):
        ui = QuantityUI(171120, 71121, target=37)
        self.assertEqual(ui.prepare()["selected"], 37)

    def test_max_failure_is_rejected_even_with_previous_selection(self):
        for selected in (1, 37, 71121):
            with self.subTest(selected=selected):
                ui = QuantityUI(171120, 71121, selected=selected)
                ui.ineffective.add("max_node")
                with self.assertRaisesRegex(RuntimeError, "MAX后仍选中1"):
                    ui.prepare()

    def test_failed_min_does_not_accept_old_selection(self):
        ui = QuantityUI(171120, 71121, selected=37)
        ui.ineffective.add("min_node")
        with self.assertRaisesRegex(RuntimeError, "MIN未回到1"):
            ui.prepare()
        self.assertNotIn("max_node", ui.actions)

    def test_max_outside_inventory_is_rejected(self):
        for inventory, maximum in [(100, 101), (171120, 171121), (100, 0)]:
            with self.subTest(inventory=inventory, maximum=maximum):
                with self.assertRaisesRegex(RuntimeError, "不在库存允许范围"):
                    QuantityUI(inventory, maximum).prepare()

    def test_sale_plan_has_no_fixed_batch_cap(self):
        self.assertEqual(quantity.plan_quantity(300000, 10000), 290000)
        self.assertEqual(quantity.plan_quantity(300000, 10000, 200000), 200000)
        self.assertEqual(quantity.plan_quantity(300000, 10000, 400000), 290000)

    def test_large_stack_respects_reserve_and_explicit_target(self):
        for reserve, target, expected in [(11410, None, 89170), (0, None, 100580),
                                          (0, 100100, 100100)]:
            with self.subTest(reserve=reserve, target=target):
                result = QuantityUI(100580, 100580, reserve=reserve, target=target).prepare()
                self.assertEqual(result["selected"], expected)
                self.assertEqual(result["maximum"], 100580)

    def test_single_remaining_item_and_ambiguous_one_item_stack(self):
        self.assertEqual(QuantityUI(1, 1).prepare()["selected"], 1)
        with self.assertRaisesRegex(RuntimeError, "无法确认当前堆叠上限"):
            QuantityUI(100000, 1).prepare()

    def test_no_sellable_quantity_never_touches_controls(self):
        for reserve, target in [(-1, None), (171120, None), (200000, None), (0, 0)]:
            with self.subTest(reserve=reserve, target=target):
                ui = QuantityUI(171120, 71121, reserve=reserve, target=target)
                self.assertEqual(ui.prepare()["status"], "skipped")
                self.assertEqual(ui.actions, [])

    def test_inventory_change_still_rejects_sale(self):
        ui = QuantityUI(171120, 71121)
        ui.change_inventory = True
        with self.assertRaisesRegex(RuntimeError, "最终库存"):
            ui.prepare()


class QuantityRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.mocks = ExitStack()
        self.addCleanup(self.mocks.close)
        self.mocks.enter_context(patch.object(quantity.time, "monotonic", side_effect=lambda: self.now))
        self.mocks.enter_context(patch.object(quantity.time, "sleep", side_effect=self.advance))
        self.mocks.enter_context(patch.object(quantity.mfaalog, "info"))

    def advance(self, seconds):
        self.now += seconds

    def ui(self):
        return QuantityUI(99319, 83438, selected=83438)

    def test_missed_click_waits_then_retries_and_reaches_exact_target(self):
        ui = self.ui()
        action = ui.action
        def miss_first(key, x=None):
            before = ui.selected
            action(key, x)
            if len(ui.actions) == 1:
                ui.selected = before
        ui.action = miss_first
        self.assertEqual(ui.adjust(83359, 83438, max_selected=83438), 83359)
        self.assertEqual(ui.actions.count("minus_ten_node"), 8)
        self.assertEqual(ui.actions.count("minus_one_node"), 9)
        self.assertAlmostEqual(self.now, 2.0)

    def test_delayed_click_is_observed_without_duplicate_input(self):
        ui = self.ui()
        action, read = ui.action, ui.read
        def delayed(key, x=None):
            before = ui.selected
            action(key, x)
            if len(ui.actions) == 1:
                ui.selected = before
        def delayed_read(full=False):
            if len(ui.actions) == 1 and self.now >= 1.2:
                ui.selected = 83428
            return read(full)
        ui.action, ui.read = delayed, delayed_read
        self.assertEqual(ui.adjust(83359, 83438, max_selected=83438), 83359)
        self.assertEqual(ui.actions.count("minus_ten_node"), 7)
        self.assertEqual(ui.actions.count("minus_one_node"), 9)
        self.assertLess(self.now, 2.0)

    def test_change_during_pre_retry_check_recomputes_step(self):
        ui = self.ui()
        ui.ineffective.add("minus_ten_node")
        read = ui.read
        def late_read(full=False):
            if full:
                # 等待结束时，游戏才给出更接近目标的数量；下一步应改用减1。
                ui.selected = 83360
            return read(full)
        ui.read = late_read
        self.assertEqual(ui.adjust(83359, 83438, max_selected=83438), 83359)
        self.assertEqual(ui.actions, ["minus_ten_node", "minus_one_node"])

    def test_permanent_miss_cancels_after_three_attempts(self):
        ui = self.ui()
        ui.ineffective.add("minus_ten_node")
        with self.assertRaisesRegex(RuntimeError, "连续3次未生效"):
            ui.adjust(83359, 83438, max_selected=83438)
        self.assertEqual(ui.actions, ["minus_ten_node"] * 3)
        self.assertEqual(ui.selected, 83438)
        self.assertAlmostEqual(self.now, 6.0)

    def test_retry_checks_detail_page_before_another_click(self):
        ui = self.ui()
        ui.ineffective.add("minus_ten_node")
        read = ui.read
        def changed_page(full=False):
            if full:
                raise RuntimeError("买卖名称未确认或不符")
            return read(full)
        ui.read = changed_page
        with self.assertRaisesRegex(RuntimeError, "名称未确认"):
            ui.adjust(83359, 83438, max_selected=83438)
        self.assertEqual(len(ui.actions), 1)

    def test_retry_preserves_stop_deadline_and_action_limit(self):
        for boundary in ("stop", "deadline", "actions"):
            with self.subTest(boundary=boundary):
                ui = self.ui()
                ui.ineffective.add("minus_ten_node")
                if boundary == "deadline":
                    ui.deadline = self.now + .7
                elif boundary == "actions":
                    ui.max_adjustments = 1
                def advance_boundary(seconds):
                    self.advance(seconds)
                    if boundary == "stop":
                        ui.context.tasker.stopping = True
                with patch.object(quantity.time, "sleep", side_effect=advance_boundary):
                    message = {"stop": "任务已停止", "deadline": "超过时限", "actions": "操作上限"}[boundary]
                    with self.assertRaisesRegex(RuntimeError, message):
                        ui.adjust(83359, 83438, max_selected=83438)
                self.assertEqual(len(ui.actions), 1)

    def test_move_away_from_target_still_cancels_immediately(self):
        ui = self.ui()
        action = ui.action
        def reverse(key, x=None):
            before = ui.selected
            action(key, x)
            ui.selected = before + 10
        ui.action = reverse
        with self.assertRaisesRegex(RuntimeError, "未接近目标"):
            ui.adjust(83359, 83438, max_selected=83438)
        self.assertEqual(len(ui.actions), 1)
        self.assertEqual(self.now, 0)

    def test_purchase_adjustment_does_not_inherit_sale_retries(self):
        ui = self.ui()
        ui.ineffective.add("minus_ten_node")
        self.assertEqual(quantity.QuantityAdjuster._adjust_step(ui, "minus_ten_node", 83438), 83438)
        self.assertEqual(len(ui.actions), 1)
        self.assertEqual(self.now, 0)


class BatchUI:
    def __init__(self, stacks, *, fail_at=None):
        self.stacks = list(stacks)
        self.tasker = SimpleNamespace(stopping=False)
        self.requests = []
        self.selected = []
        self.fail_at = fail_at

    def clone(self):
        return self

    def clear_hit_count(self, name):
        return True

    def run_task(self, entry, pipeline_override):
        request = pipeline_override[dispatch._QUANTITY]["custom_action_param"]
        self.requests.append(dict(request))
        batch = dispatch.get_batch(request["request_id"])
        ui = QuantityUI(sum(self.stacks), self.stacks[0], request=request)
        result = ui.prepare()
        batch.arm(result)
        self.selected.append(result["selected"])
        if len(self.requests) == self.fail_at:
            batch.fail("模拟成交金额未确认")
        else:
            batch.confirm({"before": 100, "after": 100 + result["quoted_total"],
                           "delta": result["quoted_total"]})
            self.stacks[0] -= result["selected"]
            if not self.stacks[0]:
                self.stacks.pop(0)
        return SimpleNamespace(status=SimpleNamespace(succeeded=True), nodes=[
            SimpleNamespace(name=dispatch._END, completed=True)])


class StackDispatchTests(unittest.TestCase):
    def setUp(self):
        self.mocks = ExitStack()
        self.addCleanup(self.mocks.close)
        for name, value in [("sync_from_context", True), ("check_scope", None)]:
            self.mocks.enter_context(patch.object(dispatch, name, return_value=value))
        self.mocks.enter_context(patch.object(dispatch.PersistentStore, "_current_account_id", "test"))
        self.mocks.enter_context(patch.object(dispatch.store, "market_day", return_value="2026-09-12"))
        self.saved = self.mocks.enter_context(patch.object(
            dispatch.store, "set_inventory_quantities", return_value=True))
        self.invalidated = self.mocks.enter_context(patch.object(
            dispatch.store, "invalidate_inventory_quantities", return_value=True))
        self.mocks.enter_context(patch.object(dispatch.gold_verify, "clear_verdict"))

    def test_large_stack_sale_completes_in_one_batch(self):
        for reserve, target, expected in [(11410, None, 89170), (0, None, 100580),
                                          (0, 100100, 100100)]:
            with self.subTest(reserve=reserve, target=target):
                ui = BatchUI([100580])
                result = dispatch.execute_sale_item(ui, "米", {}, reserve=reserve, target=target)
                self.assertEqual(result["status"], "confirmed")
                self.assertEqual(ui.selected, [expected])
                self.assertEqual(result["actual_quantity"], expected)
                self.assertEqual(sum(ui.stacks), 100580 - expected)

    def test_tail_stack_first_continues_until_reserve(self):
        ui = BatchUI([71121, 99999])
        result = dispatch.execute_sale_item(ui, "香草", {}, reserve=8400)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(ui.selected, [71121, 91599])
        self.assertEqual(result["actual_quantity"], 162720)
        self.assertEqual(ui.stacks, [8400])
        self.assertEqual(ui.requests[1]["expected_inventory"], 99999)
        self.assertEqual(ui.requests[1]["target"], 91599)
        self.saved.assert_called()
        self.invalidated.assert_not_called()

    def test_target_can_end_in_later_stack_without_expanding_plan(self):
        ui = BatchUI([29720, 99999, 99999, 99999])
        result = dispatch.execute_sale_item(ui, "香草", {}, target=150000)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(ui.selected, [29720, 99999, 20281])
        self.assertEqual(result["actual_quantity"], 150000)
        self.assertEqual(sum(ui.stacks), 179717)

    def test_confirmed_full_stack_does_not_force_another_sale(self):
        ui = BatchUI([71121, 99999])
        result = dispatch.execute_sale_item(ui, "香草", {}, reserve=99999)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(ui.selected, [71121])
        self.assertEqual(ui.stacks, [99999])

    def test_unconfirmed_later_batch_keeps_confirmed_amount_and_stops(self):
        ui = BatchUI([71121, 99999], fail_at=2)
        result = dispatch.execute_sale_item(ui, "香草", {}, reserve=8400)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["actual_quantity"], 71121)
        self.assertEqual(len(ui.requests), 2)
        self.invalidated.assert_called_once()


if __name__ == "__main__":
    unittest.main()
