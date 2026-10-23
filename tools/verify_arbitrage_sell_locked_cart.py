"""出售/补买选卡遇到未解锁卡带的回归：真实资源与内核，内存截图，不连接游戏。

修复前 Arbitrage_Sell_PackShopSwich 的 next 只有 [_Clr, 自身] 且无界。价目表会把账号尚未解锁的
卡带（如剧情游戏卡17–19）列为最高价柜台，派发过去后点击只弹「救赎游戏卡尚未解锁，无法使用商店功能。」，
该行标题是灰的，_Clr 永远认不到，节点一直 OCR→点击，也不打 [Record]
（2026-10-20 23:42–23:52 实测连点 456 次，外部看门狗 10 分钟无步骤才停）。
修复后点击后依次认：_Clr（选中）→ _Locked（缩略图上有锁）→ 自身重试；
_Locked 按入口锚点 PackSelect_Locked 退出（出售 HUB → BatchExit，补买 PackEntry → NotFound）。
自身 max_hit 5，由 Arbitrage_Sell_PackList 每轮清零；用完走默认 on_error（Global_Null）结束，
出售侧到不了 BatchEnd，主控停止后续派发；补买侧判未正常退出。

夹具 sell_list_locked.png 取自 2026-10-21 00:12 实机截图（PlayCover，1280×720），出售页，
卡带表可见活动游戏卡1/2/3/5/7 与上锁的剧情游戏卡17/18/19，顶部残留「尚未解锁」提示。
没有实机「已选中」截图：可用卡带用例在点击后于命中行下方画一条白带，代替 _Clr 认的选中效果，
只验证路由（_Clr 先于 _Locked、选中后走 PackSelect_Next）。「点不动也没有锁」的兜底用例
不画白带，模拟点击不生效。源码仓库没有 OCR 模型（选卡是 OCR），模型缺失时只跑结构检查。

运行：python -B tools/verify_arbitrage_sell_locked_cart.py -v
"""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import sys
import tempfile
import threading
import unittest

import numpy as np
from PIL import Image
from maa.controller import CustomController
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
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls), \
     patch.object(AgentServer, "tasker_sink", return_value=lambda cls: cls), \
     patch.object(AgentServer, "_set_api_properties"):
    from action import arbitrage_result as result_controller
    from action import arbitrage_sell_batch as dispatch
    from action.clear_node_hit_count import ClearNodeHitCountAction

from utils.arbitrage_cartridge import cartridge_expected
from utils.arbitrage_sale_state import LOCKED_REASON

BASE = ROOT / "assets/resource/base"
PC = ROOT / "assets/resource/pc"
FIXTURES = Path(__file__).resolve().parent / "fixtures/arbitrage_sell_locked_cart"

ENTRY = "Verify_PackSelect_Entry"
SELECTED = "Verify_PackSelect_Selected"
LOCKED_EXIT = "Verify_PackSelect_LockedExit"
PACK_LIST = "Arbitrage_Sell_PackList"
SWITCH = "Arbitrage_Sell_PackShopSwich"
CLR = "Arbitrage_Sell_PackShopSwich_Clr"
LOCKED = "Arbitrage_Sell_PackShopSwich_Locked"
FALLBACK = "Global_Null"
CLICKS_LIMIT = 5
HAS_OCR = (BASE / "model/ocr/rec.onnx").exists()
WATCHDOG_S = 90


def load(name):
    return np.ascontiguousarray(np.asarray(Image.open(FIXTURES / name).convert("RGB"))[:, :, ::-1])


class ShopListController(CustomController):
    """卡带表静止不动。selectable 为真时，点击后在命中行下方画白带，代替游戏里的选中效果；
    为假时点击不改变画面（未解锁卡带在游戏里也只弹提示）。"""

    def __init__(self, image, selectable=False):
        super().__init__()
        self.image = image
        self.selectable = selectable
        self.clicks = []

    def connect(self): return True
    def request_uuid(self): return "arbitrage-sell-locked-offline"
    def get_features(self): return 0
    def start_app(self, *args): return True
    def stop_app(self, *args): return True
    def swipe(self, *args): return True
    def touch_down(self, *args): return True
    def touch_move(self, *args): return True
    def touch_up(self, *args): return True
    def click_key(self, *args): return True
    def input_text(self, *args): return True
    def key_down(self, *args): return True
    def key_up(self, *args): return True

    def click(self, x, y):
        self.clicks.append((x, y))
        return True

    def screencap(self):
        if not (self.selectable and self.clicks):
            return self.image
        # _Clr 的 roi 是命中框下移 5、加高 20；点击点在命中框内，白带落在其下 8–20 像素
        out = self.image.copy()
        _, y = self.clicks[-1]
        out[y + 8:y + 20, 200:360] = 255
        return out


class SellLockedCartTests(unittest.TestCase):
    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_locked_cart_exits_after_one_click(self):
        for number in ("17", "18", "19"):
            with self.subTest(cart=f"剧情游戏卡{number}"):
                controller, names, failed, timed_out = self.select(f"剧情游戏卡{number}", load("sell_list_locked.png"))
                self.assertFalse(timed_out)
                self.assertFalse(failed)
                self.assertEqual(len(controller.clicks), 1)
                self.assertIn(LOCKED, names)
                self.assertEqual(names[-1], LOCKED_EXIT)
                self.assertNotIn(SELECTED, names)

    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_unlocked_cart_is_selected(self):
        controller, names, failed, timed_out = self.select("活动游戏卡7", load("sell_list_locked.png"),
                                                           selectable=True)
        self.assertFalse(timed_out)
        self.assertFalse(failed)
        self.assertEqual(len(controller.clicks), 1)
        self.assertEqual(names[-1], SELECTED)
        self.assertNotIn(LOCKED, names)

    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_unresponsive_unlocked_cart_stops_at_max_hit(self):
        # 没有锁、点了也选不中：点满上限后走默认 on_error 结束，不进选中或锁出口
        controller, names, failed, timed_out = self.select("活动游戏卡7", load("sell_list_locked.png"))
        self.assertFalse(timed_out)
        self.assertEqual(len(controller.clicks), CLICKS_LIMIT)
        self.assertEqual(names.count(SWITCH), CLICKS_LIMIT)
        self.assertEqual(names[-1], FALLBACK)
        self.assertNotIn(LOCKED, names)
        self.assertNotIn(LOCKED_EXIT, names)
        self.assertNotIn(SELECTED, names)

    def test_pack_list_resets_hit_count_across_rounds(self):
        # 同一任务内连续两轮进入 PackList：首轮点满上限后，次轮计数从 0 开始，还能再点满上限
        extra = {
            ENTRY: {"next": ["[JumpBack]Verify_PackSelect_Round1", "Verify_PackSelect_Round2"]},
            "Verify_PackSelect_Round1": {"max_hit": 1, "pre_delay": 0, "post_delay": 0, "next": [PACK_LIST]},
            "Verify_PackSelect_Round2": {"pre_delay": 0, "post_delay": 0, "next": [PACK_LIST]},
        }
        if not HAS_OCR:
            extra[SWITCH] = {"recognition": "DirectHit", "target": [260, 560, 10, 10]}
        controller, names, failed, timed_out = self.select("活动游戏卡7", load("sell_list_locked.png"),
                                                           extra=extra)
        self.assertFalse(timed_out)
        self.assertFalse(failed)
        self.assertEqual(names.count(PACK_LIST), 2)
        self.assertEqual(names.count(FALLBACK), 2)
        split = names.index(FALLBACK)
        self.assertEqual(names[:split].count(SWITCH), CLICKS_LIMIT)
        self.assertEqual(names[split + 1:].count(SWITCH), CLICKS_LIMIT)
        self.assertEqual(len(controller.clicks), CLICKS_LIMIT * 2)

    def test_locked_cart_sale_batch_is_marked_skipped(self):
        # 锁卡带从 Arbitrage_Sell_HUB 经 _Locked → BatchExit → BatchEnd 退出时，
        # SaleBatch 记为 skipped、page_ok 为真，保留原有库存，不报失败
        ui = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False),
            clone=lambda: ui,
            clear_hit_count=lambda name: True,
            get_node_object=lambda name: SimpleNamespace(attach={}),
            run_task=lambda entry, pipeline_override: SimpleNamespace(
                status=SimpleNamespace(succeeded=True),
                nodes=[
                    SimpleNamespace(name=LOCKED, completed=True),
                    SimpleNamespace(name=dispatch._END, completed=True),
                ],
            ),
        )
        market = {
            "complete": True,
            "items": [{
                "name": "草药三明治",
                "is_max_price": True,
                "target_cartridge": "剧情游戏卡17",
                "cart_score": 1.0,
                "cart_conflict": False,
                "current_rate": 130,
                "cartridge_read_basis": "direct",
            }],
        }
        logs = []
        with ExitStack() as mocks:
            for name, value in (("sync_from_context", True), ("check_scope", None)):
                mocks.enter_context(patch.object(dispatch, name, return_value=value))
            mocks.enter_context(patch.object(dispatch.PersistentStore, "_current_account_id", "test"))
            mocks.enter_context(patch.object(dispatch.store, "market_day", return_value="2026-10-21"))
            saved = mocks.enter_context(patch.object(
                dispatch.store, "set_inventory_quantities", return_value=True))
            invalidated = mocks.enter_context(patch.object(
                dispatch.store, "invalidate_inventory_quantities", return_value=True))
            mocks.enter_context(patch.object(dispatch.gold_verify, "clear_verdict"))
            mocks.enter_context(patch.object(result_controller, "market_day", return_value="2026-10-21"))
            mocks.enter_context(patch.object(result_controller, "get_market_snapshot", return_value=market))
            mocks.enter_context(patch.object(result_controller, "_load_recipe_names", return_value={"草药三明治"}))
            mocks.enter_context(patch.object(result_controller.mfaalog, "info", side_effect=logs.append))
            result = dispatch.execute_sale_item(ui, "草药三明治", {}, reserve=0)
            self.assertTrue(result_controller.ArbitrageSellController()._run(ui, {"mode": "sell"}))
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["page_ok"])
        self.assertEqual(result["actual_quantity"], 0)
        self.assertEqual(len(result["batches"]), 1)
        self.assertEqual(result["batches"][0]["status"], "skipped")
        self.assertEqual(result["batches"][0]["reason"], LOCKED_REASON)
        self.assertIsNone(result["batches"][0]["inventory_after"])
        saved.assert_not_called()
        invalidated.assert_not_called()
        self.assertIn("[Arbitrage] [草药三明治] 目标卡带尚未解锁，已跳过", logs)
        self.assertIn("[Arbitrage] 本轮1项目标卡带尚未解锁，已跳过", logs)
        self.assertTrue(any("尚未解锁" in line for line in logs))
        self.assertFalse(any("当前无可卖数量" in line for line in logs))
        self.assertFalse(any("无可卖数量" in line for line in logs))

    def test_mixed_sold_and_locked_summary_reports_both(self):
        # 同一轮里既有正常售出的物品，又有未解锁卡带跳过的物品时，汇总行同时包含两类数量
        ui = SimpleNamespace(
            tasker=SimpleNamespace(stopping=False),
            get_node_object=lambda name: SimpleNamespace(attach={}),
        )
        market = {
            "complete": True,
            "items": [
                {
                    "name": "烤蜂蜜苹果",
                    "is_max_price": True,
                    "target_cartridge": "活动游戏卡7",
                    "cart_score": 1.0,
                    "cart_conflict": False,
                    "current_rate": 130,
                    "cartridge_read_basis": "direct",
                },
                {
                    "name": "草药三明治",
                    "is_max_price": True,
                    "target_cartridge": "剧情游戏卡17",
                    "cart_score": 1.0,
                    "cart_conflict": False,
                    "current_rate": 130,
                    "cartridge_read_basis": "direct",
                },
            ],
        }
        outcomes = {
            "烤蜂蜜苹果": {
                "status": "confirmed",
                "actual_quantity": 10,
                "batches": [{"status": "confirmed", "reason": ""}],
                "page_ok": True,
            },
            "草药三明治": {
                "status": "skipped",
                "actual_quantity": 0,
                "batches": [{"status": "skipped", "reason": LOCKED_REASON}],
                "page_ok": True,
            },
        }
        logs = []
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(result_controller, "market_day", return_value="2026-10-21"))
            mocks.enter_context(patch.object(result_controller, "get_market_snapshot", return_value=market))
            mocks.enter_context(patch.object(
                result_controller, "_load_recipe_names", return_value={"烤蜂蜜苹果", "草药三明治"}))
            mocks.enter_context(patch.object(
                result_controller, "execute_sale_item",
                side_effect=lambda ctx, name, cfg, reserve=0: outcomes[name]))
            mocks.enter_context(patch.object(result_controller.mfaalog, "info", side_effect=logs.append))
            self.assertTrue(result_controller.ArbitrageSellController()._run(ui, {"mode": "sell"}))
        summary = logs[-1]
        self.assertEqual(
            summary,
            "[Arbitrage] 本轮实际售出 1 项：烤蜂蜜苹果，1项目标卡带尚未解锁，已跳过",
        )
        self.assertIn("实际售出 1 项", summary)
        self.assertIn("1项目标卡带尚未解锁，已跳过", summary)

    def test_partial_sale_then_skip_is_counted_in_summary(self):
        ui = SimpleNamespace(tasker=SimpleNamespace(stopping=False),
                             get_node_object=lambda name: SimpleNamespace(attach={}))
        market = {"complete": True, "items": [{
            "name": "草药三明治", "is_max_price": True,
            "target_cartridge": "剧情游戏卡17", "cart_score": 1.0,
            "cart_conflict": False, "current_rate": 130,
            "cartridge_read_basis": "direct",
        }]}
        for reason, ending in ((LOCKED_REASON, "1项目标卡带尚未解锁，已跳过"),
                               ("", "1项无可卖数量")):
            with self.subTest(reason=reason), ExitStack() as mocks:
                logs = []
                outcome = {"status": "skipped", "actual_quantity": 10, "page_ok": True,
                           "batches": [{"status": "confirmed", "actual_quantity": 10},
                                       {"status": "skipped", "actual_quantity": 0, "reason": reason}]}
                mocks.enter_context(patch.object(result_controller, "market_day", return_value="2026-10-21"))
                mocks.enter_context(patch.object(result_controller, "get_market_snapshot", return_value=market))
                mocks.enter_context(patch.object(result_controller, "_load_recipe_names", return_value={"草药三明治"}))
                mocks.enter_context(patch.object(result_controller, "execute_sale_item", return_value=outcome))
                mocks.enter_context(patch.object(result_controller.mfaalog, "info", side_effect=logs.append))
                self.assertTrue(result_controller.ArbitrageSellController()._run(ui, {"mode": "sell"}))
                self.assertEqual(logs[-1], f"[Arbitrage] 本轮实际售出 1 项：草药三明治，{ending}")

    def test_pipeline_shape(self):
        base = json.loads((BASE / "pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        pc = json.loads((PC / "pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        self.assertEqual(base[SWITCH]["next"], [CLR, LOCKED, SWITCH])
        self.assertEqual(base[SWITCH]["max_hit"], CLICKS_LIMIT)
        self.assertEqual(base[PACK_LIST]["custom_action"], "ClearNodeHitCount")
        self.assertIn(SWITCH, base[PACK_LIST]["custom_action_param"]["node_name"])
        self.assertEqual(base[LOCKED]["roi"], SWITCH)
        self.assertEqual(base[LOCKED]["next"], ["[Anchor]PackSelect_Locked"])
        self.assertTrue((BASE / "image" / base[LOCKED]["template"][0]).exists())
        # 每个设 PackSelect_Next 的入口都给锁出口；清 PackSelect_Next 的出口一并清掉
        for name, node in base.items():
            anchor = node.get("anchor") if isinstance(node, dict) else None
            if not isinstance(anchor, dict) or "PackSelect_Next" not in anchor:
                continue
            with self.subTest(node=name):
                self.assertIn("PackSelect_Locked", anchor)
                self.assertEqual(bool(anchor["PackSelect_Next"]), bool(anchor["PackSelect_Locked"]))
        self.assertEqual(base["Arbitrage_Sell_HUB"]["anchor"]["PackSelect_Locked"], "Arbitrage_Sell_Item_BatchExit")
        self.assertEqual(base["Arbitrage_PreciseBuy_PackEntry"]["anchor"]["PackSelect_Locked"],
                         "Arbitrage_PreciseBuy_NotFound")
        # PC 包只覆盖 roi，流程字段继承 base
        self.assertIn(SWITCH, pc)
        self.assertEqual(pc[SWITCH].get("roi"), [140, 85, 170, 610])
        for field in ("next", "max_hit", "on_error"):
            self.assertNotIn(field, pc[SWITCH])
        self.assertNotIn(LOCKED, pc)

    def select(self, cart, image, selectable=False, extra=None):
        fixture = {
            # 代替 HUB / PreciseBuy 入口：只设两个锚点，然后走真实的 PackList → 选卡
            ENTRY: {"pre_delay": 0, "post_delay": 0, "next": [PACK_LIST],
                    "anchor": {"PackSelect_Next": SELECTED, "PackSelect_Locked": LOCKED_EXIT}},
            SELECTED: {"pre_delay": 0, "post_delay": 0},
            LOCKED_EXIT: {"pre_delay": 0, "post_delay": 0},
            PACK_LIST: {"post_delay": 0},
            SWITCH: {"expected": cartridge_expected(cart), "post_delay": 0, "rate_limit": 0, "timeout": 0},
            CLR: {"timeout": 0},
        }
        for name, fields in (extra or {}).items():
            fixture.setdefault(name, {}).update(fields)
        with tempfile.TemporaryDirectory(prefix="mfabd2-sell-locked-", ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(directory)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            resource = Resource()
            tasker = Tasker()
            controller = ShopListController(image, selectable)
            # 修复前会一直点，看门狗停掉后交给断言报错
            fired = threading.Event()
            watchdog = threading.Timer(WATCHDOG_S, lambda: (fired.set(), tasker.post_stop()))
            try:
                self.assertTrue(resource.post_bundle(BASE).wait().succeeded)
                self.assertTrue(resource.override_pipeline(fixture))
                self.assertTrue(resource.register_custom_action("ClearNodeHitCount", ClearNodeHitCountAction()))
                self.assertTrue(controller.post_connection().wait().succeeded)
                self.assertTrue(tasker.bind(resource, controller))
                watchdog.start()
                detail = tasker.post_task(ENTRY).wait().get()
                names = [node.name for node in detail.nodes if node and node.name] if detail else []
                failed = detail is None or detail.status.failed
                return controller, names, failed, fired.is_set()
            finally:
                watchdog.cancel()
                tasker.post_stop().wait()
                del tasker
                resource.clear()
                Tasker.set_log_dir("")


if __name__ == "__main__":
    unittest.main()
