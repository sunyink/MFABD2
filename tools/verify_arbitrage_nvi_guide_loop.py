"""跑商寻路按钮死循环回归：真实资源与内核，内存截图，不连接游戏。

修复前 Arbitrage_NviGuide_Butt 的 next 含自身且无界：游戏不响应点击时寻路列表打不开，
按钮一直认得到，任务永不结束（2026-10-19 12:07–12:10 实测连点 172 次，重启游戏后恢复）。
修复后 Butt 自身 max_hit 11，用完由 on_error 直接进 Global_Null_Panic 重启游戏；
Arbitrage_Sandplay_QuickMove 每次寻路前清零 Butt 的计数，重启后重新寻路仍能进入 Butt。
Panic 用满 3 次后 on_error 无节点接住，任务判失败。

Panic 保留 StopApp 与 max_hit，把 next 换成 QuickMove：代替「StartGame 走完、退栈回调用方、
重新寻路到商人」这一段。真实的退栈回接没有在这里覆盖。

夹具取自 2026-10-19 实机截图，只保留寻路按钮与列表区域，其余填平色：
- nvi_list_closed.png：卡住时的画面，列表关着；
- nvi_list_open.png：重启前手动点开的列表，含「商店」。列表打开后按钮模板认不到。
源码仓库没有 OCR 模型，认「商店」的用例在模型缺失时跳过。

运行：python -B tools/verify_arbitrage_nvi_guide_loop.py -v
"""

from pathlib import Path
from unittest.mock import patch
import importlib.util
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

# 单独加载这一个文件：导入 action 包会连带注册全部识别器与 sink
_spec = importlib.util.spec_from_file_location("clear_node_hit_count", ROOT / "agent/action/clear_node_hit_count.py")
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls):
    _spec.loader.exec_module(_module)
ClearNodeHitCountAction = _module.ClearNodeHitCountAction

BASE = ROOT / "assets/resource/base"
PC = ROOT / "assets/resource/pc"
FIXTURES = Path(__file__).resolve().parent / "fixtures/arbitrage_nvi_guide_loop"

SEE = "Arbitrage_Sandplay_QuickMove"
BUTT = "Arbitrage_NviGuide_Butt"
MERCHANT = "Arbitrage_NviGuide_Merchant_Ocr"
PANIC = "Global_Null_Panic"
CLICKS_LIMIT = 11  # Butt 的 max_hit：1 次 + 10 次补点
PANIC_LIMIT = 3
BUTT_ROI = [208, 72, 138, 43]
HAS_OCR = (BASE / "model/ocr/rec.onnx").exists()
WATCHDOG_S = 90


def load(name):
    return np.ascontiguousarray(np.asarray(Image.open(FIXTURES / name).convert("RGB"))[:, :, ::-1])


class NviGuideController(CustomController):
    """本次启动里寻路按钮点满 opens_after 次后列表打开；None 表示游戏不响应点击，列表永远关着。
    StopApp 视为重启：清空本次启动的点击，之后按 after_restart 响应。"""

    def __init__(self, opens_after, after_restart=None):
        super().__init__()
        self.closed = load("nvi_list_closed.png")
        self.open = load("nvi_list_open.png")
        self.opens_after = opens_after
        self.after_restart = after_restart
        self.clicks = []
        self.total_butt_clicks = 0
        self.restarts = 0

    def connect(self): return True
    def request_uuid(self): return "arbitrage-nvi-guide-offline"
    def get_features(self): return 0
    def start_app(self, *args): return True
    def swipe(self, *args): return True
    def touch_down(self, *args): return True
    def touch_move(self, *args): return True
    def touch_up(self, *args): return True
    def click_key(self, *args): return True
    def input_text(self, *args): return True
    def key_down(self, *args): return True
    def key_up(self, *args): return True

    def stop_app(self, *args):
        self.restarts += 1
        self.clicks = []
        self.opens_after = self.after_restart
        return True

    def click(self, x, y):
        self.clicks.append((x, y))
        self.total_butt_clicks += in_butt(x, y)
        return True

    def butt_clicks(self):
        return sum(in_butt(cx, cy) for cx, cy in self.clicks)

    def screencap(self):
        opened = self.opens_after is not None and self.butt_clicks() >= self.opens_after
        return self.open if opened else self.closed


def in_butt(x, y):
    bx, by, bw, bh = BUTT_ROI
    return bx <= x < bx + bw and by <= y < by + bh


class NviGuideLoopTests(unittest.TestCase):
    def test_unresponsive_game_restarts_then_fails(self):
        # 每次重启后重新寻路都清零计数，点满 11 次；第 4 轮 Panic 已用满，任务失败
        controller, names, failed, timed_out = self.run_nav(NviGuideController(None))
        self.assertFalse(timed_out)
        self.assertEqual(controller.restarts, PANIC_LIMIT)
        self.assertEqual(names.count(PANIC), PANIC_LIMIT)
        self.assertEqual(controller.total_butt_clicks, CLICKS_LIMIT * (PANIC_LIMIT + 1))
        self.assertEqual(names[-1], BUTT)
        self.assertTrue(failed)

    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_restart_restores_clicks(self):
        controller, names, failed, timed_out = self.run_nav(NviGuideController(None, after_restart=1))
        self.assertFalse(timed_out)
        self.assertEqual(controller.restarts, 1)
        self.assertEqual(controller.total_butt_clicks, CLICKS_LIMIT + 1)
        self.assertEqual(names[-1], MERCHANT)
        self.assertFalse(failed)

    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_list_opens_first_click(self):
        controller, names, failed, timed_out = self.run_nav(NviGuideController(1))
        self.assertFalse(timed_out)
        self.assertEqual(controller.total_butt_clicks, 1)
        self.assertEqual(names[-1], MERCHANT)
        self.assertNotIn(PANIC, names)

    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_dropped_clicks_are_retried(self):
        controller, names, failed, timed_out = self.run_nav(NviGuideController(3))
        self.assertFalse(timed_out)
        self.assertEqual(controller.total_butt_clicks, 3)
        self.assertEqual(names.count(BUTT), 3)
        self.assertEqual(names[-1], MERCHANT)
        self.assertNotIn(PANIC, names)

    def test_open_list_hides_button(self):
        # 列表已经打开时按钮认不到，不会再点把它关上
        controller = NviGuideController(1)
        controller.clicks.append((BUTT_ROI[0] + 1, BUTT_ROI[1] + 1))
        controller, names, failed, timed_out = self.run_nav(controller, {SEE: {"timeout": 0}})
        self.assertFalse(timed_out)
        self.assertEqual(controller.total_butt_clicks, 0)
        self.assertNotIn(BUTT, names)

    def test_pipeline_shape(self):
        base = json.loads((BASE / "pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        pc = json.loads((PC / "pipeline/Arbitrage.json").read_text(encoding="utf-8"))
        dummy = json.loads((BASE / "pipeline/Dummy.json").read_text(encoding="utf-8"))
        self.assertEqual(base[SEE]["custom_action"], "ClearNodeHitCount")
        self.assertEqual(base[SEE]["custom_action_param"], {"node_name": BUTT})
        self.assertEqual(base[SEE]["next"], [BUTT])
        self.assertEqual(base[BUTT]["next"], [MERCHANT, BUTT])
        self.assertEqual(base[BUTT]["max_hit"], CLICKS_LIMIT)
        self.assertEqual(base[BUTT]["on_error"], [PANIC])
        self.assertIn("timeout", base[BUTT])
        self.assertEqual(dummy[PANIC]["max_hit"], PANIC_LIMIT)
        # PC 包只覆盖 roi/模板，流程字段继承 base
        for field in ("next", "on_error", "max_hit", "timeout"):
            self.assertNotIn(field, pc.get(BUTT, {}))
        self.assertNotIn(SEE, pc)
        # 入口借 Butt 的识别，PC 包改 Butt 的 roi/模板时入口跟着走
        self.assertEqual(base[SEE]["any_of"], [BUTT])

    def run_nav(self, controller, extra=None):
        fixture = {
            # 入口只认寻路按钮，直接命中；保留清计数动作
            SEE: {"recognition": "DirectHit", "pre_delay": 0, "post_delay": 0},
            BUTT: {"pre_delay": 0, "rate_limit": 0},
            MERCHANT: {"action": "DoNothing", "next": []},
            # 保留 StopApp 与 max_hit；next 代替「重启后退栈、重新寻路到商人」
            PANIC: {"post_delay": 0, "next": [SEE]},
        }
        for name, fields in (extra or {}).items():
            fixture[name].update(fields)
        # MaaFramework keeps its log handle open until process exit on Windows.
        with tempfile.TemporaryDirectory(prefix="mfabd2-arbnav-", ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(directory)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            resource = Resource()
            tasker = Tasker()
            # 修复前按钮会被一直点，看门狗停掉后交给断言报错
            fired = threading.Event()
            watchdog = threading.Timer(WATCHDOG_S, lambda: (fired.set(), tasker.post_stop()))
            try:
                self.assertTrue(resource.post_bundle(BASE).wait().succeeded)
                self.assertTrue(resource.override_pipeline(fixture))
                self.assertTrue(resource.register_custom_action("ClearNodeHitCount", ClearNodeHitCountAction()))
                self.assertTrue(controller.post_connection().wait().succeeded)
                self.assertTrue(tasker.bind(resource, controller))
                watchdog.start()
                detail = tasker.post_task(SEE).wait().get()
                # 轮询超时、无节点接住时留下空名记录，只保留命中的节点
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
