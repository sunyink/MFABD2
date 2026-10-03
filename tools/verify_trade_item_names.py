"""Trade item-name wiring regressions; captured text, native routing, no game/save access.

Run: .venv/Scripts/python -B tools/verify_trade_item_names.py -v

Wiring inventory:
- Market/possession names: arbitrage_pricelist.read_name (existing).
- Purchase favorites: ShopBuyFavController._read_names -> _bind_star_to_name.
- Sale list: _sell_item_override -> OCRItemName name slots (type-tag anchored cards).
- Precise-buy list: buy_overrides -> OCRItemName over the whole list.
- Sale, purchase confirmation and sale retry details: QuantityAdjuster.inventory_state.
- Bag stock names: BagScanner.inspect (existing).
Cartridge names, configured identities, template-derived material names and
cooking recipe-title metadata are outside this trade-name change.
"""

from functools import partial
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import numpy as np
from maa.context import Context
from maa.custom_action import CustomAction
from maa.define import LoggingLevelEnum
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer

with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "_set_api_properties"), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls):
    from action import shop_buy_fav_controller as favorites
    from action import arbitrage_sell_batch as sale
    from action.arbitrage_sell_quantity import QuantityAdjuster
    from action.arbitrage_buy_precise import BuyQuantityAdjuster, buy_overrides
    from action.arbitrage_result import _sell_item_override
    from recognition.ocr_score import OCRItemName
from utils import ocr_item_name as names
from utils.sale_name_slots import slot_config
from verify_ocr_score import CaptureBox, Fallback, OfflineController

SOURCE = "Agt_<Sell_Item>_Ocr"
SELECTOR = "Agt_<Sell_Item>_OcrScore"
PARENT = "Arbitrage_Sell_Item_ListTraverse"
TEMPLATE = "Agt_<Sell_Item>_Tmp"
SLOTS = "Agt_SellList_NameSlot"
PIPE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
NAME_NODE = PIPE[SLOTS]["attach"]["name_node"]
SAMPLES = [("鲑魚芥末壽司", "三文鱼芥末寿司"), ("紅蘿萄", "胡萝卜"),
           ("蘿萄嬰", "萝卜缨"), ("包装好的海苔", "调味海苔")]
IMAGE = np.zeros((720, 1280, 3), dtype=np.uint8)


def candidate(text, score=.99, box=(396, 117, 90, 20)):
    return NS(text=text, score=score, box=list(box))


def tag(x, y, text="食物"):
    return candidate(text, 1.0, (x, y, 36, 20))


def slot_of(card):
    dx, dy, w, h = PIPE[SLOTS]["attach"]["slot"]
    return [card.box[0] + dx, card.box[1] + dy, w, h]


def observed(*items, raw=()):
    return NS(hit=bool(items), reco_id=123, filtered_results=list(items), all_results=list(raw))


def context_for(result):
    return NS(tasker=NS(stopping=False),
              get_node_data=lambda n: {"recognition": {"type": "OCR", "param": PIPE.get(n, {})}},
              get_node_object=lambda n: NS(attach=PIPE[n].get("attach", {})),
              run_recognition=Mock(return_value=result))


def slot_context(listed, slot_reads):
    """Whole-list OCR plus per-slot reads keyed by slot ROI; local rereads find nothing."""
    def run(node, image, override):
        if override.get(node, {}).get("only_rec"):
            return observed()
        if node == NAME_NODE:
            return observed(*slot_reads.get(tuple(override[node]["roi"]), []))
        return observed(*listed)
    ctx = context_for(None)
    ctx.run_recognition = Mock(side_effect=run)
    return ctx


def sale_pick(ctx, target):
    return OCRItemName().analyze(ctx, NS(image=IMAGE, custom_recognition_param={
        "node": SOURCE, "item_name": target, "name_slots": SLOTS}))


class ItemWiringTests(unittest.TestCase):
    def test_recorded_names_reach_favorites_list_and_both_detail_paths(self):
        for raw, expected in SAMPLES:
            with self.subTest(raw=raw):
                row = candidate(raw)
                ctx = context_for(observed(row))
                picker = OCRItemName().analyze(ctx, NS(image=IMAGE, custom_recognition_param={
                    "node": SOURCE, "item_name": expected}))
                self.assertEqual(picker.box, row.box)
                self.assertEqual(picker.detail["names"][0]["name"], expected)
                ctx.run_recognition.assert_called_once_with(SOURCE, IMAGE, {SOURCE: {"expected": []}})
                fav = favorites.ShopBuyFavController()
                fav.cfg = {"name_max_len": 7, "bind_dx_min": 5, "bind_dx_max": 40, "bind_dy_max": 15}
                fav._scan_issues = []
                found = fav._read_names(observed(row), set(), ctx, IMAGE)
                stars = [{"box": [371, 117, 17, 17], "right_x": 388, "cy": 127,
                          "cx": 380, "color": "gray"}]
                entities = fav._bind_star_to_name(stars, found)
                self.assertTrue(fav._complete_page(entities))
                self.assertEqual(fav._decide_actions(entities, {expected})[0]["action"], "light")
                for cls in (QuantityAdjuster, BuyQuantityAdjuster):
                    adjuster = object.__new__(cls)
                    adjuster.context, adjuster.name = ctx, expected
                    adjuster.config = {"name_node": "name"}
                    adjuster.recognize = Mock(return_value=observed(row))
                    adjuster.texts = Mock(return_value=["持有4,235個"])
                    self.assertEqual(adjuster.inventory_state(IMAGE), 4235)

    def test_resource_replacement_is_consumed_and_unverified_favorites_are_preserved(self):
        fav = favorites.ShopBuyFavController()
        fav.cfg = {"name_max_len": 7, "bind_dx_min": 5, "bind_dx_max": 40, "bind_dy_max": 15}
        fav._scan_issues = []
        corrected = fav._read_names(observed(candidate("蘿蔔嬰"), raw=[candidate("蘿萄嬰")]), set())
        self.assertEqual(corrected[0]["resolution"]["basis"], "exact")
        unknown = fav._read_names(observed(candidate("完全未知商品")), set())
        entities = fav._bind_star_to_name(
            [{"box": [371, 117, 17, 17], "right_x": 388, "cy": 127, "cx": 380, "color": "yellow"}], unknown)
        self.assertFalse(fav._complete_page(entities))
        self.assertEqual(fav._decide_actions(entities, {"萝卜缨"}), [])

    def test_all_shop_catalog_names_including_books_can_be_resolved(self):
        shops = json.loads((ROOT / "agent/data/arbitrage_shop_catalog.json").read_text(encoding="utf-8"))
        for name in {name for shop in shops["cartridges"].values() for name in shop["items"]}:
            self.assertTrue(names.resolve_ocr_name(name)["confirmed"], name)

    def test_full_catalog_ambiguity_cannot_be_broken_by_target_and_blocks_detail(self):
        resolver = partial(names.resolve_ocr_name, aliases={"香甜面包": "巧克力面包", "香甜蛋糕": "手工蛋糕"})
        ctx = context_for(observed(candidate("香甜面糕")))
        with patch.object(names, "resolve_ocr_name", side_effect=resolver):
            selected = OCRItemName().analyze(ctx, NS(image=IMAGE, custom_recognition_param={
                "node": SOURCE, "item_name": "巧克力面包"}))
            self.assertIsNone(selected.box)
            self.assertTrue(selected.detail["unconfirmed_names"])
            adjuster = object.__new__(QuantityAdjuster)
            adjuster.context, adjuster.name, adjuster.config = ctx, "巧克力面包", {"name_node": "name"}
            adjuster.recognize = Mock(return_value=observed(candidate("香甜面糕")))
            adjuster.texts = Mock()
            with self.assertRaises(ValueError):
                adjuster.inventory_state(IMAGE)
            adjuster.texts.assert_not_called()

    def test_low_score_is_reread_and_short_wrong_name_is_not_guessed(self):
        ctx = context_for(observed(candidate("鮭魚芥末壽司")))
        self.assertTrue(names.resolve_item_ocr(ctx, SOURCE, IMAGE, candidate(SAMPLES[0][0], .4))["confirmed"])
        self.assertEqual(ctx.run_recognition.call_count, 2)
        for call in ctx.run_recognition.call_args_list:
            self.assertIs(call.args[1], IMAGE)
            self.assertTrue(call.args[2][SOURCE]["only_rec"])
        ctx = context_for(observed(candidate("米", .4)))
        selected = OCRItemName().analyze(ctx, NS(image=IMAGE, custom_recognition_param={"node": SOURCE, "item_name": "米"}))
        self.assertIsNone(selected.box)

    def test_catalog_valid_other_item_is_never_changed_to_requested_item(self):
        ctx = context_for(observed(candidate("鱼子酱蛋包饭")))
        selected = OCRItemName().analyze(ctx, NS(image=IMAGE, custom_recognition_param={
            "node": SOURCE, "item_name": "三文鱼芥末寿司"}))
        self.assertIsNone(selected.box)
        self.assertFalse(selected.detail["unconfirmed_names"])

    def test_uncertainty_on_earlier_page_prevents_zero_inventory_at_end(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                reads = iter([
                    NS(hit=True), NS(hit=False, raw_detail={"detail": [{"detail": {"all": [{"detail": {
                        "unconfirmed_names": ["未确认"] if uncertain else []}}]}}]}), NS(hit=False),
                    NS(hit=True), NS(hit=False, raw_detail={}), NS(hit=True)])
                ctx = NS(tasker=NS(controller=NS(post_screencap=lambda: NS(wait=lambda: NS(get=lambda: IMAGE)))),
                         get_node_object=lambda _: NS(attach={"max_pages": 2}), get_anchor=lambda _: "end",
                         run_recognition=lambda *args: next(reads),
                         run_task=lambda _: NS(status=NS(succeeded=True), nodes=[
                             NS(name="Arbitrage_ItemList_Swip", action=NS(success=True))]))
                batch = NS(request={"item_name": "三文鱼芥末寿司"}, absent=Mock(), fail=Mock())
                with patch.object(sale, "_batch", return_value=batch), patch.object(sale, "check_scope"):
                    self.assertFalse(sale.ArbitrageSaleSearch().run(ctx, NS(node_name="search")))
                self.assertEqual(batch.absent.call_count, 0 if uncertain else 1)
                self.assertEqual(batch.fail.call_count, 1 if uncertain else 0)


class NameSlotTests(unittest.TestCase):
    """Sale search reads only the name box above each type tag (10-03 sale-page layout)."""

    def test_list_noise_outside_name_slots_cannot_block_zero_inventory(self):
        card = tag(477, 167)
        name = candidate("蘑菇汤", .99, (476, 142, 54, 22))
        listed = [card, name,
                  candidate("茶", .3, (447, 142, 16, 11)),        # cooking star rating
                  candidate("96%美茶", .8, (404, 142, 59, 14)),   # rate badge glued to the rating
                  candidate("元86", .83, (476, 188, 55, 17)),     # coin icon on the price line
                  candidate("售", .99, (440, 190, 20, 11)),       # half of a sold-out badge
                  candidate("◆食物", .99, (392, 111, 51, 22))]    # ADB section heading
        picked = sale_pick(slot_context(listed, {tuple(slot_of(card)): [name]}), "三文鱼芥末寿司")
        self.assertIsNone(picked.box)
        self.assertEqual(picked.detail["unconfirmed_names"], [])
        self.assertEqual([n["name"] for n in picked.detail["names"]], ["蘑菇汤"])
        self.assertFalse(names.has_unconfirmed_item_name(picked.detail))
        legacy = OCRItemName().analyze(slot_context(listed, {}), NS(image=IMAGE, custom_recognition_param={
            "node": SOURCE, "item_name": "三文鱼芥末寿司"}))
        self.assertTrue(legacy.detail["unconfirmed_names"])

    def test_target_in_name_slot_returns_the_slot_read_box(self):
        card = tag(477, 167)
        name = candidate("鲑魚芥末壽司", .95, (476, 143, 90, 20))
        ctx = slot_context([card], {tuple(slot_of(card)): [name]})
        picked = sale_pick(ctx, "三文鱼芥末寿司")
        self.assertEqual(picked.box, name.box)
        ctx.run_recognition.assert_any_call(NAME_NODE, IMAGE, {NAME_NODE: {"roi": slot_of(card), "expected": []}})

    def test_heading_spelled_like_a_tag_is_not_a_card(self):
        # ADB after.png 09-11: the 食物 heading at the list edge, its ◆ cut by the ROI.
        heading, card = candidate("食物", 1.0, (392, 111, 51, 22)), tag(477, 167)
        name = candidate("蘑菇汤", .99, (476, 142, 54, 22))
        ctx = slot_context([heading, card, name], {tuple(slot_of(card)): [name]})
        picked = sale_pick(ctx, "三文鱼芥末寿司")
        self.assertEqual(picked.detail["tag_count"], 1)
        self.assertFalse(names.has_unconfirmed_item_name(picked.detail))

    def test_without_any_tag_column_texts_are_still_checked(self):
        picked = sale_pick(slot_context([candidate("未知料理", .9, (477, 300, 70, 20))], {}), "三文鱼芥末寿司")
        self.assertEqual([u["raw"] for u in picked.detail["unconfirmed_names"]], ["未知料理"])

    def test_tagged_card_without_a_readable_name_blocks_zero_inventory(self):
        card = tag(477, 167)
        picked = sale_pick(slot_context([card], {}), "三文鱼芥末寿司")
        self.assertEqual(picked.detail["unconfirmed_names"][0]["basis"], "empty_name_slot")
        self.assertTrue(names.has_unconfirmed_item_name(picked.detail))

    def test_cards_cut_at_list_edges_and_missed_tags(self):
        cut, left, middle, right = tag(477, 100), tag(477, 188), tag(743, 188), tag(1009, 276)
        listed = [cut, left, middle, right,
                  candidate("未知料理", .9, (1009, 164, 70, 20)),   # row of `left`, its tag was missed
                  candidate("甜辣鲜虾", .99, (477, 340, 70, 20)),   # below the last row, tag cut off
                  candidate("茶", .3, (447, 340, 16, 11)),          # star rating beside it
                  candidate("元87", .83, (1009, 297, 55, 17))]      # price line of the last tagged row
        reads = {tuple(slot_of(card)): [candidate(text, .99, (slot_of(card)[0] + 6, slot_of(card)[1] + 4, 60, 20))]
                 for card, text in ((left, "蘑菇汤"), (middle, "米"), (right, "白糖"))}
        ctx = slot_context(listed, reads)
        picked = sale_pick(ctx, "三文鱼芥末寿司")
        detail = picked.detail
        self.assertEqual((detail["tag_count"], detail["top_cut"], detail["extra_count"]), (4, 1, 2))
        self.assertEqual([u["raw"] for u in detail["unconfirmed_names"]], ["未知料理"])
        self.assertIn("甜辣鲜虾", [n["name"] for n in detail["names"]])
        slot_calls = [c for c in ctx.run_recognition.call_args_list if c.args[0] == NAME_NODE]
        self.assertNotIn(slot_of(cut)[1], [c.args[2][NAME_NODE]["roi"][1] for c in slot_calls])

    def test_name_slot_is_clipped_to_the_list_top(self):
        card = tag(477, 112)
        ctx = slot_context([card], {})
        sale_pick(ctx, "三文鱼芥末寿司")
        rois = [c.args[2][NAME_NODE]["roi"] for c in ctx.run_recognition.call_args_list if c.args[0] == NAME_NODE]
        dx, dy, w, h = PIPE[SLOTS]["attach"]["slot"]
        self.assertEqual(rois, [[477 + dx, 94, w, 112 + dy + h - 94]])

    def test_precise_buy_keeps_whole_list_reading(self):
        ctx = context_for(None)
        sale_param = _sell_item_override(ctx, "米")[SELECTOR]["custom_recognition_param"]
        buy_param = buy_overrides(ctx, {"item_name": "米", "cartridge": "剧情游戏卡1"})[SELECTOR][
            "custom_recognition_param"]
        self.assertEqual(sale_param["name_slots"], SLOTS)
        self.assertNotIn("name_slots", buy_param)

    def test_shipped_layout_is_valid_and_bad_layout_stops_zero_inventory(self):
        cfg = slot_config(PIPE[SLOTS]["attach"])
        for word in ("食物", "材料", "升級", "升级"):
            self.assertTrue(cfg["tag_re"].fullmatch(word), word)
        self.assertFalse(cfg["tag_re"].fullmatch("料理食材"))
        self.assertEqual(len(cfg["columns"]), 3)  # base = ADB layout; PC overrides with four columns
        self.assertEqual(PIPE[cfg["name_node"]]["recognition"], "OCR")
        self.assertFalse(PIPE[cfg["name_node"]].get("only_rec", False))
        broken = dict(PIPE[SLOTS]["attach"], slot=[-6, -28, 0, 26])
        ctx = slot_context([tag(477, 167)], {})
        ctx.get_node_object = lambda n: NS(attach=broken if n == SLOTS else PIPE[n].get("attach", {}))
        picked = sale_pick(ctx, "三文鱼芥末寿司")
        self.assertIsNone(picked.box)
        self.assertTrue(picked.detail["name_read_failed"])


class NativeItemRoutingTests(unittest.TestCase):
    def test_actual_sale_and_buy_overrides_route_corrected_box_through_or(self):
        with tempfile.TemporaryDirectory(prefix="mfabd2-trade-names-", ignore_cleanup_errors=True) as logdir:
            Tasker.set_log_dir(logdir)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            resource = Resource()
            self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
            picker, capture, fallback = OCRItemName(), CaptureBox(), Fallback()
            self.assertTrue(resource.register_custom_recognition("OCRItemName", picker))
            self.assertTrue(resource.register_custom_recognition("test_fallback", fallback))
            self.assertTrue(resource.register_custom_action("test_capture", capture))
            controller = OfflineController()
            self.assertTrue(controller.post_connection().wait().succeeded)
            tasker = Tasker()
            self.assertTrue(tasker.bind(resource, controller))
            try:
                for pc in (False, True):
                    if pc:
                        if not (ROOT / "assets/resource/pc/pipeline/Arbitrage.json").is_file():
                            continue
                        self.assertTrue(resource.post_bundle(ROOT / "assets/resource/pc").wait().succeeded)
                    for buying, (raw, expected) in [(buying, sample) for buying in (False, True) for sample in SAMPLES]:
                        with self.subTest(pc=pc, buying=buying, raw=raw):
                            ctx = context_for(None)
                            overrides = (buy_overrides(ctx, {"item_name": expected, "cartridge": "剧情游戏卡1"})
                                         if buying else _sell_item_override(ctx, expected))
                            overrides[PARENT].update(action="Custom", custom_action="test_capture", next=[],
                                                     pre_delay=0, post_delay=0, timeout=1, rate_limit=0)
                            overrides[TEMPLATE] = {"recognition": "Custom", "custom_recognition": "test_fallback"}
                            def source(context, node, image, override):
                                if node == NAME_NODE:  # sale only: the name box above the tag
                                    self.assertFalse(buying)
                                    return observed(candidate(raw))
                                self.assertEqual(node, SOURCE)
                                self.assertEqual(override[SOURCE]["expected"], [])
                                column = context.get_node_object(SLOTS).attach["columns"][0]
                                return observed(candidate(raw), tag(column, 137))
                            with patch.object(Context, "run_recognition", source):
                                result = tasker.post_task(PARENT, overrides).wait().get()
                            self.assertTrue(result.status.succeeded)
                            self.assertEqual(capture.boxes[-1], candidate(raw).box)
                            self.assertEqual(fallback.calls, 0)
                # Use the real Or/Custom result wrappers to verify that uncertainty
                # reaches the sale search, rather than relying only on fake details.
                captured = []
                class Inspect(CustomAction):
                    def run(self, context, argv):
                        result = context.run_recognition(PARENT, IMAGE)
                        captured.append(result)
                        return True
                self.assertTrue(resource.register_custom_action("test_inspect_names", Inspect()))
                overrides = _sell_item_override(context_for(None), "三文鱼芥末寿司")
                overrides[PARENT]["any_of"] = [SELECTOR]
                overrides["test_inspect"] = {"action": "Custom", "custom_action": "test_inspect_names", "next": []}
                original = Context.run_recognition
                def unknown(context, node, image, *args, **kwargs):
                    if node == SOURCE:
                        column = context.get_node_object(SLOTS).attach["columns"][0]
                        return observed(candidate("未知料理名称"), tag(column, 137))
                    if node == NAME_NODE:
                        return observed(candidate("未知料理名称"))
                    return original(context, node, image, *args, **kwargs)
                with patch.object(Context, "run_recognition", unknown):
                    self.assertTrue(tasker.post_task("test_inspect", overrides).wait().succeeded)
                self.assertFalse(captured[-1].hit)
                self.assertTrue(names.has_unconfirmed_item_name(captured[-1].raw_detail))
            finally:
                tasker.post_stop().wait()
                del tasker
                del controller
                resource.clear()
                Tasker.set_log_dir("")


if __name__ == "__main__":
    unittest.main()
