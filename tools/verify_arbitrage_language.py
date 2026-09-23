"""Shared shop language regressions: real resource merges, no game or account writes.

Run: .venv/Scripts/python -B tools/verify_arbitrage_language.py -v
"""

from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.define import LoggingLevelEnum

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer

with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "_set_api_properties"), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls):
    from action.arbitrage_buy_precise import buy_overrides
    from action.arbitrage_sell_quantity import parse_quantity
    from action.shop_buy_fav_controller import ShopBuyFavController
from utils import arbitrage_cartridge as cart
from utils.name_i18n import canon
from utils.ocr_item_name import item_aliases


BASE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text("utf-8"))
PC = json.loads((ROOT / "assets/resource/pc/pipeline/Arbitrage.json").read_text("utf-8"))
# Real UI wording and known translations, independent of the patterns under test.
UI_CASES = {
    "Arbitrage_NviGuide_CKMsgBox_Ocr": ["确认", "確認"],
    "Arbitrage_Bargaining_SkillList": ["砍价", "砍價", "討價還價"],
    "Arbitrage_Bargaining_Msgbox_Ck": ["砍价", "討價還價"],
    "Arbitrage_Merchant_Egress": ["购买", "購買", "出售", "販售"],
    "Arbitrage_Merchant_Close_Ck": ["关闭商店", "關閉商店"],
    "Arbitrage_Buy_Button_Chg": ["购买", "購買"],
    "Arbitrage_PackList_ResetEnter": ["血骑士", "血騎士"],
    "Arbitrage_Favorit_Buy_SubMenu": ["购买全部收藏", "購買全部收藏"],
    "Arbitrage_Favorit_Buy_SubMenu_BuyAction": ["确认", "確認"],
    "Arbitrage_Favorit_Buy_Empty": ["没有可以购买的商品", "沒有可以購買的商品"],
    "Arbitrage_Merchant_BuyMsg_Button": ["购买", "購買"],
    "Arbitrage_PriceList_Open_Entry": ["价目表", "價目表", "販售行情表"],
    "Arbitrage_PriceList_Open_Egress": ["价目表", "販售行情表"],
    "Arbitrage_PriceList_Sort_Select": ["溢价率", "溢價率", "加價率"],
    "Arbitrage_ItemList_Sorting_Type_Not_Change": ["溢价率", "加價率"],
    "Arbitrage_Sell_Item_ListTraverse_End": ["圣石", "聖石", "装备材料", "裝備材料", "装備材料"],
    "Arbitrage_Sell_Item_ListTraverse_End_Recipes": ["圣石", "聖石", "裝備材料", "料理食材"],
    "Rec_<Arbitrage_Sell_Item_SellMenu>_Ocr_01": ["出售", "販售"],
    "Arbitrage_Sell_Item_Selling": ["出售", "販售"],
    "Arbitrage_SellB_Button": ["一键出售", "一鍵販售"],
    "Arbitrage_SellB_SubMenu": ["溢价率", "加價率"],
    "Arbitrage_Cooking_Menu_Reset_SubOut": ["所需材料", "需要的材料"],
    "Arbitrage_Cooking_Menu_ReBack": ["所需材料", "需要的材料"],
    "Arbitrage_Cooking_SubMenu": ["拥有", "擁有", "持有", "立刻恢复", "立即恢復", "1個食物"],
    "Arbitrage_BagStockScan_Sort_Menu": ["优先", "優先", "排序"],
    "Arbitrage_BagStockScan_Sort_Menu_Set": ["料理食材优先", "料理食材優先", "料理食材由多至少排序"],
    "Arbitrage_BagStockScan_Sort_Egress": ["按料理食材数量从低到高排序", "料理食材由少至多排序"],
    "Agt_BuyQuantity_Available_Ocr": ["可购买400个", "可購買400個", "可购買400個"],
    "Agt_BuyConfirm_Ocr": ["购买", "購買", "购買"],
    "Arbitrage_Sell_Item_ListReset_Sell_Chg": ["出售", "販售"],
    "Arbitrage_Sell_Item_ListReset_PostOcr": ["食物", "料理"],
    "Rec_<Arbitrage_Merchant_Dialog>_Ocr": ["天赋技能", "天賦技能"],
}
COMPOSITES = {
    "Arbitrage_Favorit_Buy": (0, ["购买全部收藏", "一鍵購買全部收藏"]),
    "Arbitrage_PriceList_Sort_Check": (0, ["从高到低", "從高到低", "降序"]),
    "Arbitrage_ItemList_Sorting_Reverse": (0, ["从高到低", "降序"]),
    "Arbitrage_Bag_TabReset_Coll": (1, ["收集品", "收藏品"]),
    "Arbitrage_Bag_TabReset_Cons": (1, ["消耗品"]),
    "Agt_BagStock_List_Ready": (1, ["料理食材优先", "料理食材優先"]),
}
SHOPS = {
    "QC": ["血骑士|血騎士", "苍蓝魔女|蒼藍魔女", "迷雾神射手|迷霧神射手", "眼镜与猫|眼鏡與貓",
           "沙漠之花", "异教塔|異教塔", "愤怒天使|憤怒的天使", "血之狂想曲", "铁假面|鐵面具",
           "霍尔蒙克斯|霍爾蒙克斯", "虚假游戏|虛假遊戲", "黑羽毛", "雪之歌", "神圣审判|神聖審判",
           "复仇的誓言|復仇的誓約", "三国同盟|三國同盟", "试炼之路|試煉之路", "救赎|救贖",
           "被遗忘的战争|被遺忘的戰爭"],
    "QR": ["杰登之门|傑登之門", "火晶片|火片", "美丽无望|不可能的美麗", "大逃脱|大逃脫",
           "鲁的迷宫|魯的迷宮", "御剑传|御劍傳", "合约之战|合約之戰"],
    "QE": ["夏日骑士|夏天騎士", "恶梦之冬|惡夢之冬", "海滨天使|海邊天使", "记忆边缘|記憶邊緣",
           "戏水女王|戲水女王"],
}


def matches(patterns, text):
    return any(re.search(pattern, text) for pattern in patterns)


class LanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryDirectory(prefix="arbitrage-language-", ignore_cleanup_errors=True)
        Tasker.set_log_dir(cls.logs.name)
        Tasker.set_stdout_level(LoggingLevelEnum.Off)
        cls.resources = []
        for pc in (False, True):
            resource = Resource()
            assert resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded
            if pc:
                assert resource.post_bundle(ROOT / "assets/resource/pc").wait().succeeded
            cls.resources.append(resource)

    @classmethod
    def tearDownClass(cls):
        cls.resources.clear()
        Tasker.set_log_dir("")
        cls.logs.cleanup()

    def test_ui_and_all_31_shop_names_in_both_resource_loads(self):
        cases = dict(UI_CASES)
        for family, names in SHOPS.items():
            for number, name in enumerate(names, 1):
                cases[f"Arbitrage_Buy_Select_{family}{number}"] = name.split("|")
        self.assertEqual(sum(map(len, SHOPS.values())), 31)
        for pc, resource in enumerate(self.resources):
            for node, samples in cases.items():
                patterns = resource.get_node_data(node)["recognition"]["param"]["expected"]
                for text in samples:
                    with self.subTest(pc=pc, node=node, text=text):
                        self.assertTrue(matches(patterns, text))

    def test_composite_recognition_uses_shared_words_with_platform_roi(self):
        for parent, (index, samples) in COMPOSITES.items():
            helper = f"Rec_<{parent}>_Ocr"
            for pc, resource in enumerate(self.resources):
                children = resource.get_node_data(parent)["recognition"]["param"]["all_of"]
                self.assertEqual(children[index], helper)
                params = resource.get_node_data(helper)["recognition"]["param"]
                self.assertEqual(params["roi"], (PC.get(helper) if pc else BASE[helper])["roi"])
                for text in samples:
                    self.assertTrue(matches(params["expected"], text), (parent, pc, text))
            self.assertNotIn("next", BASE[helper])
            self.assertNotIn("on_error", BASE[helper])

    def test_pc_does_not_override_language_fields(self):
        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ("expected", "replace", "type_labels", "type_patterns", "ocr_exclude"):
                        self.assertIsNone(re.search(r"[\u3400-\u9fff]", json.dumps(child, ensure_ascii=False)))
                    else:
                        walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(PC)
        for resource in self.resources:
            config = resource.get_node_data(cart.RESCUE_NODE)["attach"]
            self.assertEqual(config["type_patterns"], cart.DEFAULT_CONFIG["type_patterns"])
            self.assertEqual(config["type_labels"], cart.DEFAULT_CONFIG["type_labels"])

    def test_issue_514_old_cache_names_match_real_ocr_without_mutation(self):
        # Issue #514, 00:26:17.223: this OCR result scored 0.999985 but was filtered out.
        actual = "剧情游戏卡14"
        caches = [{"target_cartridge": value} for value in
                  ("故事遊戲卡帶14", "故事游戏卡带14", "剧情游戏卡14", "劇情遊戲卡14")]
        before = deepcopy(caches)
        for resource in self.resources:
            context = NS(get_node_object=lambda n: NS(attach=resource.get_node_data(n).get("attach", {})))
            cfg = cart.load_config(context)
            for row in caches:
                rules = cart.cartridge_expected(row["target_cartridge"], cfg)
                self.assertIsInstance(rules, list)
                self.assertTrue(matches(rules, actual))
                self.assertTrue(matches(rules, "故事遊戲卡帶14"))
                self.assertTrue(matches(rules, "剧情游戏卡14 神圣审判"))
                for wrong in ("剧情游戏卡1", "剧情游戏卡4", "剧情游戏卡114", "剧情游戏卡014",
                              "角色游戏卡14"):
                    self.assertFalse(matches(rules, wrong), wrong)
            self.assertEqual(cart.cartridge_identity(caches[0]["target_cartridge"], cfg), ("story", "14"))
        self.assertEqual(caches, before)

    def test_types_ranges_and_unknown_values(self):
        for label in ("剧情游戏卡", "劇情遊戲卡", "故事遊戲卡帶"):
            for number in range(1, 20):
                self.assertEqual(cart.cartridge_identity(label + str(number)), ("story", str(number)))
        for kind, labels in {"character": ["角色游戏卡", "角色遊戲卡帶"],
                             "event": ["活动游戏卡", "活動遊戲卡帶", "活动遊戏卡帶"]}.items():
            for label in labels:
                for number in range(1, 8):
                    self.assertEqual(cart.cartridge_identity(label + str(number)), (kind, str(number)))
        for text in ("14", "剧情14", "故事游戏卡", "故事游戏卡0", "故事游戏卡20", "角色游戏卡8",
                     "活动游戏卡01", "故事角色游戏卡14", "未知游戏卡14", "店长游戏卡1"):
            self.assertIsNone(cart.cartridge_identity(text), text)
            with self.assertRaises(ValueError):
                cart.cartridge_expected(text)
        # Manager names are covered without extending its currently empty number range.
        for text in ("店长游戏卡", "店長遊戲卡帶"):
            self.assertTrue(matches(cart.DEFAULT_CONFIG["type_patterns"]["manager"], text))

    def test_buy_dispatch_uses_resource_patterns_and_rejects_invalid_cartridge(self):
        context = NS(get_node_object=lambda n: NS(attach=BASE[n].get("attach", {})))
        for source in ("剧情游戏卡14", "故事遊戲卡帶14"):
            override = buy_overrides(context, {"item_name": "蜂蜜黄油杏仁", "cartridge": source})
            expected = override["Arbitrage_Sell_PackShopSwich"]["expected"]
            self.assertTrue(matches(expected, "剧情游戏卡14"))
            self.assertTrue(matches(expected, "故事遊戲卡帶14"))
        with self.assertRaises(ValueError):
            buy_overrides(context, {"item_name": "蜂蜜黄油杏仁", "cartridge": "未知14"})
        cfg = cart.load_config(NS(get_node_object=lambda n: NS(attach={
            "type_patterns": {"story": ["[", ""]}})))
        self.assertEqual(cfg["type_patterns"], cart.DEFAULT_CONFIG["type_patterns"])

    def test_sale_dispatch_deduplicates_old_language_aliases_and_skips_unknown_type(self):
        from action import arbitrage_result as ar
        from verify_arbitrage_preview import Context, Argv, item
        for source, alternate, calls in [("故事遊戲卡帶14", "剧情游戏卡14", 1),
                                         ("剧情游戏卡14", "故事遊戲卡帶13", 2),
                                         ("未知游戏卡14", "", 0)]:
            context = Context()
            controller = ar.ArbitrageSellController()
            row = {**item("烤蜂蜜苹果", cart=source), "alt_cartridge": alternate, "cart_conflict": True}
            scan = {"items": [row], "peak_items": [row], "complete": True}
            with patch.object(ar, "sync_from_context", return_value=True), \
                 patch.object(ar, "get_market_snapshot", return_value=None), \
                 patch.object(ar, "save_possession_snapshot", return_value=True), \
                 patch.object(controller, "_scan_price_list", return_value=scan), \
                 patch.object(ar, "execute_sale_item", return_value={
                     "status": "skipped", "actual_quantity": 0, "page_ok": True}) as execute:
                self.assertTrue(controller.run(context, Argv('{"mode":"preview_possess"}')))
            self.assertEqual(execute.call_count, calls, (source, alternate))
            if calls:
                rules = execute.call_args_list[0].args[2]["Arbitrage_Sell_PackShopSwich"]["expected"]
                self.assertTrue(matches(rules, "剧情游戏卡14"))
                self.assertTrue(matches(rules, "故事遊戲卡帶14"))

    def test_inventory_filter_and_confirmation_keep_strict_boundaries(self):
        for text in ("拥有153,730个", "擁有153,730個", "持有１５３，７３０個", "拥有153,730個"):
            self.assertEqual(parse_quantity([text], inventory=True), 153730)
        for values in (["擁有6"], ["擁有6.5個"], ["可購買6個"], ["擁有6個", "持有7個"]):
            with self.assertRaises(ValueError):
                parse_quantity(values, inventory=True)
        excluded = ShopBuyFavController()._parse_item_list(BASE["Arbitrage_ShopBuy_Data_Csm"]["attach"]["ocr_exclude"])
        for text in ("购买", "購買", "販售", "贩售", "还剩", "還剩"):
            self.assertIn(text, excluded)
        self.assertNotIn("可購買400個", excluded)
        for resource in self.resources:
            params = resource.get_node_data("Arbitrage_Sell_Item_Selling")["recognition"]["param"]
            self.assertFalse(matches(params["expected"], "出售全部收藏"))
            self.assertFalse(matches(params["expected"], "販售行情表"))

    def test_recipe_and_shop_dictionaries_cover_both_complete_names(self):
        items = json.loads((ROOT / "agent/data/bd2_item_names_i18n.json").read_text("utf-8"))
        shops = json.loads((ROOT / "agent/data/arbitrage_shop_catalog.json").read_text("utf-8"))["cartridges"]
        allowed = {name for shop in shops.values() for name in shop["items"]}
        aliases = item_aliases()
        for item in items:
            if item.get("category") != "Recipe" and item["cn"] not in allowed:
                continue
            self.assertTrue(item.get("tw"), item["cn"])
            self.assertEqual(canon(item["tw"]), item["cn"])
            self.assertEqual(aliases[item["tw"]], item["cn"])


if __name__ == "__main__":
    unittest.main()
