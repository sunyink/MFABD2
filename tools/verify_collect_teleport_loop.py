"""地图采集区域地图点传送阵的死循环回归：真实资源与内核，内存截图，不连接游戏。

修复前 White（点首选）与 PurpleProcess（点次选）的 next 都含自身且无界：点中的图标不传送时
（灰色未激活阵等），任务永不结束。紫色判定 count=1 又分不清两种实机画面：
- 血骑士 tp2：首选可用、次选点了没反应，图标边缘的杂色像素时有时无（10-12、10-19 卡在次选）；
- 沙漠之花 tp2：首选是灰阵、次选可用，紫色像素每次都在（10-19 把阈值调高后卡在首选）。
修复后每次选阵先清零计数，点了不传送时同一图标最多补点 3 次，再换另一个图标一次，都用完就结束本卡带。

夹具取自实机截图，只保留标题、右换页箭头与图标几块，其余填平色：
- area_map_stray_pixel.png：2026-10-18 施塔因之塔第 1 层，单个可用图标，(622,332) 是被当成紫色的灰像素；
- area_map_inactive_first.png：2026-10-19 沙漠之花 tp2，首选 [560,318] 灰阵，次选 [647,310] 可用。

运行：python -B tools/verify_collect_teleport_loop.py -v
"""

from pathlib import Path
import importlib.util
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from maa.controller import CustomController
from maa.define import LoggingLevelEnum
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures/collect_teleport_loop"
sys.path.insert(0, str(ROOT / "agent"))
Library.version()
from maa.agent.agent_server import AgentServer
# Native tests register callbacks on Resource, not on an AgentServer process.
# 只加载 counter.py，不经 recognition/__init__ 拉起整个识别包
with patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_action", return_value=lambda cls: cls):
    _spec = importlib.util.spec_from_file_location("counter", ROOT / "agent/recognition/counter.py")
    counter = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(counter)

ENTRY = "Collect_ClickTeleporCircleIco"
PURPLE = "Collect_ClickTelepor_TargetClass_Purple"
FIRST = "Collect_ClickTelepor_TargetClass_White"
SECOND = "Collect_ClickTelepor_TargetClass_PurpleProcess"
TO_FIRST = "Collect_ClickTelepor_Switch_ToFirst"
TO_SECOND = "Collect_ClickTelepor_Switch_ToSecond"
END = "Collect_OperationsMain_OperationsEnd"
SKILL_ENTRY = "Collect_Skill_Entry"
CLICKS_PER_ICON = 4  # 1 次 + CheckTag max 3 次补点
ARRIVED_MARK = [1117, 639, 53, 51]  # Rec_SandBox_Pin_Tpl 的 roi，到达箱庭后白块出现
WATCHDOG_S = 60

STEIN_ICON = [601, 318, 29, 28]
STEIN_DEAD_COPY_DX = 200
DESERT_GRAY = [560, 318, 29, 28]
DESERT_ACTIVE = [647, 310, 29, 28]
DESERT_PURPLE_PIXELS = [(569, 325), (571, 341)]


def load(name):
    return np.ascontiguousarray(np.asarray(Image.open(FIXTURES / name).convert("RGB"))[:, :, ::-1])


def stein_with_dead_second():
    # 血骑士型：首选可用，右侧复制一个点了没反应的次选；(622,332) 的灰像素让紫色判定误中
    frame = load("area_map_stray_pixel.png")
    x, y, w, h = STEIN_ICON
    dx = STEIN_DEAD_COPY_DX
    frame[y - 10:y + h + 10, x - 10 + dx:x + w + 10 + dx] = frame[y - 10:y + h + 10, x - 10:x + w + 10].copy()
    return frame, [STEIN_ICON], [STEIN_ICON[0] + dx, *STEIN_ICON[1:]]


def desert_without_purple_pixels():
    # 沙漠之花型且紫色像素没出现（杂色时有时无）：只剩「首选是灰阵」这一条件
    frame = load("area_map_inactive_first.png")
    for x, y in DESERT_PURPLE_PIXELS:
        frame[y, x] = frame[y, x - 1]
    return frame


def inside(box, point):
    x, y, w, h = box
    return x <= point[0] < x + w and y <= point[1] < y + h


class AreaMapController(CustomController):
    """区域地图画面：点中 working 里的图标才传送，其余图标怎么点画面都不变。"""

    def __init__(self, frame, working):
        super().__init__()
        self.frame = frame
        self.working = working
        self.arrived = np.full_like(frame, 60)
        x, y, w, h = ARRIVED_MARK
        self.arrived[y:y + h, x:x + w] = 255
        self.clicks = []

    def connect(self): return True
    def request_uuid(self): return "collect-teleport-loop-offline"
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
        if any(inside(box, point) for box in self.working for point in self.clicks):
            return self.arrived
        return self.frame


class TeleportLoopTests(unittest.TestCase):
    def test_purple_misjudged_dead_second_switches_to_first(self):
        frame, working, dead = stein_with_dead_second()
        controller, names, timed_out = self.run_entry(frame, working)
        self.assertFalse(timed_out)
        self.assertIn(PURPLE, names)
        self.assertEqual(names.count(SECOND), CLICKS_PER_ICON)
        self.assertIn(TO_FIRST, names)
        self.assertEqual(names[-1], SKILL_ENTRY)
        self.assertEqual(sum(inside(dead, c) for c in controller.clicks), CLICKS_PER_ICON)

    def test_inactive_first_switches_to_second(self):
        controller, names, timed_out = self.run_entry(desert_without_purple_pixels(), [DESERT_ACTIVE])
        self.assertFalse(timed_out)
        self.assertNotIn(PURPLE, names)
        self.assertEqual(names.count(FIRST), CLICKS_PER_ICON)
        self.assertIn(TO_SECOND, names)
        self.assertEqual(names[-1], SKILL_ENTRY)
        self.assertEqual(sum(inside(DESERT_GRAY, c) for c in controller.clicks), CLICKS_PER_ICON)

    def test_both_dead_ends_cartridge(self):
        controller, names, timed_out = self.run_entry(desert_without_purple_pixels(), [])
        self.assertFalse(timed_out)
        self.assertEqual(names.count(FIRST), CLICKS_PER_ICON)
        self.assertEqual(names.count(SECOND), CLICKS_PER_ICON)
        self.assertEqual(names[-1], END)

    def test_working_first_needs_no_retry(self):
        frame, _, _ = stein_with_dead_second()
        x, y = 622, 332
        frame[y, x] = frame[y, x - 1]  # 去掉杂色像素：紫色判定不中，直接点首选
        controller, names, timed_out = self.run_entry(frame, [STEIN_ICON])
        self.assertFalse(timed_out)
        self.assertEqual(names.count(FIRST), 1)
        self.assertNotIn(TO_SECOND, names)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def run_entry(self, frame, working):
        counter.TAG_STORE.clear()
        fixture = {
            FIRST: {"post_delay": 0, "rate_limit": 0},
            SECOND: {"post_delay": 0, "rate_limit": 0},
            # 内存截图没有箱庭 UI，用右下角白块代表「已到达」
            SKILL_ENTRY: {"recognition": "ColorMatch", "roi": ARRIVED_MARK, "lower": [250, 250, 250],
                          "upper": [255, 255, 255], "count": ARRIVED_MARK[2] * ARRIVED_MARK[3], "next": []},
            # 源码仓库没有 OCR 模型，标题 OCR 换成右换页箭头模板，同样代表「仍在区域地图上」
            END: {"any_of": ["Rec_AreaMap_ButtRight_Tmp"], "action": "DoNothing", "next": [], "post_delay": 0},
        }
        # MaaFramework keeps its log handle open until process exit on Windows.
        with tempfile.TemporaryDirectory(prefix="mfabd2-teleport-", ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(directory)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            resource = Resource()
            controller = AreaMapController(frame, working)
            tasker = Tasker()
            # 修复前点不动的图标会被一直点，看门狗停掉后交给断言报错
            fired = threading.Event()
            watchdog = threading.Timer(WATCHDOG_S, lambda: (fired.set(), tasker.post_stop()))
            try:
                self.assertTrue(resource.post_bundle(ROOT / "assets/resource/base").wait().succeeded)
                self.assertTrue(resource.override_pipeline(fixture))
                self.assertTrue(resource.register_custom_recognition("CheckTag", counter.CheckTag()))
                self.assertTrue(resource.register_custom_action("UpdateTag", counter.UpdateTag()))
                self.assertTrue(resource.register_custom_action("ResetTag", counter.ResetTag()))
                self.assertTrue(controller.post_connection().wait().succeeded)
                self.assertTrue(tasker.bind(resource, controller))
                watchdog.start()
                detail = tasker.post_task(ENTRY).wait().get()
                return controller, [node.name for node in detail.nodes if node], fired.is_set()
            finally:
                watchdog.cancel()
                tasker.post_stop().wait()
                del tasker
                resource.clear()
                Tasker.set_log_dir("")


if __name__ == "__main__":
    unittest.main()
