"""料理短缺采集回归检查；可用 --images <截图...> 运行原生识别，不连接游戏。

运行：.venv/Scripts/python tools/verify_cooking_stock.py [--images ...]
截图需为 1280x720 料理详情页。回放只执行采集节点，清空其后续动作。
"""

import argparse
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from maa.library import Library

Library.version()  # 在导入 maa.agent 前固定为本地 MaaFramework 模式。
spec = importlib.util.spec_from_file_location("cooking_stock", ROOT / "agent/action/cooking_stock.py")
stock = importlib.util.module_from_spec(spec)
# 单进程回放用 Resource 注册动作，避免装饰器提前把库切到 AgentServer 模式。
with patch("maa.agent.agent_server.AgentServer.custom_action", return_value=lambda cls: cls):
    spec.loader.exec_module(stock)

PIPELINE = json.loads((ROOT / "assets/resource/base/pipeline/Arbitrage.json").read_text(encoding="utf-8"))
ENTRY = "Arbitrage_Cooking_MaterialInsufficient"
PARAMS = PIPELINE[ENTRY]["custom_action_param"]


class FakeContext:
    def __init__(self, texts=None):
        self.texts = texts or {}

    def get_node_object(self, name):
        node = PIPELINE[name]
        return SimpleNamespace(attach=node.get("attach", {}),
                               recognition=SimpleNamespace(param=SimpleNamespace(roi=node["roi"])))

    def run_recognition(self, name, image):
        if name in PARAMS["presence_nodes"]:
            return SimpleNamespace(hit=PARAMS["presence_nodes"].index(name) < 3)
        texts = self.texts.get(name, [])
        return SimpleNamespace(hit=bool(texts), filtered_results=[SimpleNamespace(text=t) for t in texts])


class CookingStockTests(unittest.TestCase):
    def setUp(self):
        stock._OBSERVATIONS.clear()

    def test_numeric_reading_does_not_invent_zero_or_join_fragments(self):
        pattern = stock._NUMBER + "/" + stock._NUMBER
        self.assertEqual(stock._unique_parsed(["０／２"], pattern), (0, 2))
        self.assertEqual(stock._unique_parsed(["60,264/4"], pattern), (60264, 4))
        for texts in ([], ["0", "/2"], ["0/2", "8/2"], ["60.264/4"], ["0264/4"]):
            self.assertIsNone(stock._unique_parsed(texts, pattern))

    def test_task_isolation_and_latest_incomplete_observation(self):
        stock._save(1, {"recipe": "酱炒牛排", "complete": True})
        stock._save(2, {"recipe": "酱炒牛排", "complete": True})
        stock._save(1, {"recipe": "酱炒牛排", "complete": False})
        self.assertFalse(stock.get_cooking_stock(1)[0]["complete"])
        self.assertTrue(stock.get_cooking_stock(2)[0]["complete"])
        self.assertEqual(stock.get_cooking_stock(3), [])
        copy = stock.get_cooking_stock(2)
        copy.clear()
        self.assertEqual(len(stock.get_cooking_stock(2)), 1)

    def test_partial_ocr_and_empty_slots(self):
        texts = {PARAMS["recipe_node"]: ["酱炒牛排 ***", "★"], PARAMS["quantity_node"]: ["1个"]}
        for node, value in zip(PARAMS["material_nodes"], ["60264/4", "0/2", "4290/2"]):
            texts[node] = [value]
        candidates = [[{"name": name, "score": 0.99}] for name in ("兽肉", "西蓝花", "盐")] + [[], []]
        with patch.object(stock, "_match_materials", return_value=candidates):
            record = stock.collect_cooking_stock(FakeContext(texts), PARAMS, object())
            self.assertTrue(record["complete"], record)
            self.assertEqual(record["recipe"], "酱炒牛排")
            self.assertEqual(len(record["materials"]), 3)
            self.assertEqual(record["materials"][1]["stock"], 0)
            texts[PARAMS["material_nodes"][1]] = []
            partial = stock.collect_cooking_stock(FakeContext(texts), PARAMS, object())
            self.assertFalse(partial["complete"])
            self.assertIsNone(partial["materials"][1]["stock"])
            self.assertEqual(partial["materials"][2]["stock"], 4290)
            texts[PARAMS["quantity_node"]] = ["10个"]
            batch = stock.collect_cooking_stock(FakeContext(texts), PARAMS, object())
            self.assertTrue(all(item["per_craft"] is None for item in batch["materials"]))

    def test_template_catalog_and_pipeline_wiring(self):
        templates = PIPELINE[PARAMS["template_node"]]["attach"]["templates"]
        self.assertEqual(len(templates), len(set(templates.values())))
        for path in templates.values():
            self.assertTrue((ROOT / "assets/resource/base/image" / path).is_file(), path)
        for node in ("Arbitrage_Cooking_SubMenu", "Arbitrage_Cooking_NubMenu_Max", "Arbitrage_Cooking_Doing_ReBack"):
            self.assertEqual(PIPELINE[node]["next"][0], ENTRY)
        for node in PARAMS["material_nodes"] + [PARAMS["recipe_node"], PARAMS["quantity_node"]]:
            self.assertNotIn("only_rec", PIPELINE[node])
            self.assertNotIn("next", PIPELINE[node])

    def test_green_mask_recipe_entries_are_disabled_until_selected(self):
        recipes = {
            "Arbitrage_Cooking_B11": "洛克菲勒生蚝",
            "Arbitrage_Cooking_B12": "冰镇甜点",
            "Arbitrage_Cooking_B13": "炸猪排盖饭",
            "Arbitrage_Cooking_B14": "橄榄油意面",
            "Arbitrage_Cooking_B15": "汉堡排便当",
            "Arbitrage_Cooking_B16": "泰瑞丝派",
            "Arbitrage_Cooking_B17": "三明治便当",
            "Arbitrage_Cooking_B18": "手工蛋糕",
            "Arbitrage_Cooking_B19": "香甜马卡龙",
            "Arbitrage_Cooking_B20": "街头烤鸡肉串",
            "Arbitrage_Cooking_B21": "香草牛排",
        }
        page_two = PIPELINE["Arbitrage_Cooking_Swip_Page2"]["next"]
        for node, name in recipes.items():
            self.assertIn(node, page_two)
            data = PIPELINE[node]
            self.assertFalse(data["enabled"])
            matcher = data["all_of"][0]
            self.assertTrue(matcher["green_mask"])
            self.assertEqual(matcher["template"], [f"Shop/RecipeList/料理_{name}.png"])
            self.assertTrue((ROOT / "assets/resource/base/image" / matcher["template"][0]).is_file())

    def test_recipe_pages_cover_selectors_and_five_star_options(self):
        page_two = PIPELINE["Arbitrage_Cooking_Swip_Page2"]["next"]
        page_one = PIPELINE["Arbitrage_Cooking_Hub"]["next"]
        selectors = {name for name, node in PIPELINE.items()
                     if node.get("next") == ["Arbitrage_Cooking_SubMenu"]}
        self.assertEqual(len(selectors), 25)
        self.assertEqual(len(page_two), 16)
        self.assertEqual(len(page_one), 9)
        self.assertEqual(len(page_two + page_one), len(set(page_two + page_one)))
        self.assertEqual(set(page_two + page_one), selectors)
        self.assertNotIn("Arbitrage_Cooking_A6", PIPELINE)
        interface = json.loads((ROOT / "assets/interface.json").read_text(encoding="utf-8"))
        cases = interface["option"]["5星料理开关"]["cases"]
        self.assertEqual({name for case in cases for name in case["pipeline_override"]}, set(page_two))
        for name in selectors:
            node = PIPELINE[name]
            self.assertEqual("[5级]" in node["focus"], name in page_two)
            self.assertEqual(node.get("enabled", True), name in page_one)
            self.assertEqual(node["max_hit"], 1)
            for template in node["all_of"][0]["template"]:
                self.assertTrue((ROOT / "assets/resource/base/image" / template).is_file(), template)
        self.assertEqual(PIPELINE["Arbitrage_Cooking_MenuEnter"]["next"], [
            "[JumpBack]Arbitrage_Cooking_Page2", "[JumpBack]Arbitrage_Cooking_Page1"])


def replay(images, negative_images):
    import numpy as np
    from PIL import Image
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from maa.define import LoggingLevelEnum

    class ScreenshotController(CustomController):
        def __init__(self, path):
            with Image.open(path) as source:
                self.image = np.asarray(source.convert("RGB"))[:, :, ::-1].copy()
            assert self.image.shape[:2] == (720, 1280)
            super().__init__()

        def connect(self):
            return True

        def request_uuid(self):
            return "cooking-stock-offline"

        def screencap(self):
            return self.image.copy()

        def reject_input(self, *args):
            raise AssertionError("Screenshot replay must never issue input")

        start_app = stop_app = click = swipe = touch_down = touch_move = touch_up = reject_input
        click_key = input_text = key_down = key_up = reject_input

    with tempfile.TemporaryDirectory(prefix="cooking-stock-verify-", ignore_cleanup_errors=True) as scratch:
        Tasker.set_log_dir(scratch)
        Tasker.set_stdout_level(LoggingLevelEnum.Off)
        resource = Resource()
        assert resource.post_bundle(ROOT / "assets/resource/base").wait().status.succeeded
        assert resource.register_custom_action("CookingStockSnapshot", stock.CookingStockSnapshot())
        for image, should_trigger in [(path, True) for path in images] + [(path, False) for path in negative_images]:
            controller = ScreenshotController(image.resolve())
            assert controller.post_connection().wait().status.succeeded
            tasker = Tasker()
            assert tasker.bind(resource, controller)
            before = time.perf_counter()
            # 从父节点的 next 触发，验证真实候选分支；非红色画面应直接走空出口。
            result = tasker.post_task("test_cooking_stock", {
                "test_cooking_stock": {"next": [ENTRY, "test_cooking_stock_end"],
                                       "pre_delay": 0, "post_delay": 0},
                "test_cooking_stock_end": {"pre_delay": 0, "post_delay": 0},
                ENTRY: {"next": [], "on_error": [], "focus": {}},
            }).wait().get()
            assert result is not None and result.status.succeeded, image
            records = stock.get_cooking_stock(result.task_id)
            print(json.dumps({"image": image.name, "seconds": round(time.perf_counter() - before, 3),
                              "triggered": bool(records)}, ensure_ascii=False))
            if should_trigger:
                assert len(records) == 1 and records[0]["complete"], image
            else:
                assert records == [], image
        # Release native objects before cleaning up their log directory.
        del tasker, controller, resource
        Tasker.set_log_dir("")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", nargs="+", type=Path)
    parser.add_argument("--negative-images", nargs="+", type=Path, help="不应触发采集的料理详情截图")
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CookingStockTests))
    if not result.wasSuccessful():
        sys.exit(1)
    if args.images or args.negative_images:
        replay(args.images or [], args.negative_images or [])
