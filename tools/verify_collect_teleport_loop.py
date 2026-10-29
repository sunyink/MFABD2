"""地图采集区域地图选传送阵的回归：真实资源与内核，内存截图，不连接游戏。

选阵规则：先点白阵（已激活），从左到右最多 3 个；没有能用的白阵才点紫阵（传送技能放的临时阵）；
灰阵（未激活）永不点击。白阵、紫阵各靠 HSV 色核从模板候选里筛出，白、紫模板互相串匹配，不能只看模板。
每个阵点了不传送就重点，max_hit 4 用完换下一个；全部用完或本页没有可点的阵，就放弃本页往右翻。
入口 Collect_ClickTeleporCircleIco 每次选阵用 ClearNodeHitCount 清零四个点阵节点的命中次数。

历史：#566 之前 White / PurpleProcess 的 next 自环无界，count=1 的 RGB 紫色判定会把图标边缘的
[144,144,145] 灰像素当紫，血骑士 tp2 卡在次选、沙漠之花 tp2 卡在灰阵。

夹具取自实机 PNG 截图，只保留标题、右换页箭头与图标区，其余填平色：
- area_map_stray_pixel.png：施塔因之塔第 1 层，单个白阵，(622,332) 是旧紫色判定误中的灰像素；
- area_map_inactive_first.png：沙漠之花 tp2，左灰阵 [560,318]、右白阵 [647,310]；
- area_map_purple_two_white.png：2026-10-26 战斗Ⅰ 亚拉里克遗迹，紫阵 [509,347]、白阵 [559,318] 与 [647,310]；
- pc_area_map_two_gray.png：2026-10-26 PC，两个灰阵 [565,361]、[645,280]；
- pc_area_map_white_purple.png：2026-10-26 PC，紫阵 [597,285]、白阵 [632,333]。

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


def load_module(name, path):
    # Native tests register callbacks on Resource, not on an AgentServer process.
    # 只加载单个模块，不经 action/__init__ 拉起整个 agent
    with patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
         patch.object(AgentServer, "custom_action", return_value=lambda cls: cls):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


clear_hit = load_module("clear_node_hit_count", ROOT / "agent/action/clear_node_hit_count.py")

ENTRY = "Collect_ClickTeleporCircleIco"
WHITE = "Collect_ClickTelepor_TargetClass_White"
WHITE2 = "Collect_ClickTelepor_TargetClass_White2"
WHITE3 = "Collect_ClickTelepor_TargetClass_White3"
PURPLE = "Collect_ClickTelepor_TargetClass_Purple"
CLICKERS = [WHITE, WHITE2, WHITE3, PURPLE]
GIVE_UP = "Collect_ClickTelepor_GiveUp"
END = "Collect_OperationsMain_OperationsEnd"
SKILL_ENTRY = "Collect_Skill_Entry"
CLICKS_PER_ICON = 4  # 点阵节点 max_hit
ARRIVED_MARK = [1117, 639, 53, 51]  # Rec_SandBox_Pin_Tpl 的 roi，到达箱庭后白块出现
WATCHDOG_S = 120

BASE = ["base"]
PC = ["base", "pc"]

STEIN_WHITE = [601, 318, 29, 28]
DESERT_GRAY = [560, 318, 29, 28]
DESERT_WHITE = [647, 310, 29, 28]
RUIN_PURPLE = [509, 347, 29, 28]
RUIN_WHITE1 = [559, 318, 29, 28]
RUIN_WHITE2 = [647, 310, 29, 28]
PC_GRAYS = [[565, 361, 19, 20], [645, 280, 19, 20]]
PC_PURPLE = [597, 285, 19, 20]
PC_WHITE = [632, 333, 19, 20]


def load(name):
    return np.ascontiguousarray(np.asarray(Image.open(FIXTURES / name).convert("RGB"))[:, :, ::-1])


def desert_gray_only():
    # 沙漠之花 tp2 把右边白阵涂成底色：只剩灰阵
    frame = load("area_map_inactive_first.png")
    x, y, w, h = DESERT_WHITE
    frame[y - 12:y + h + 12, x - 12:x + w + 12] = frame[5, 5]
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
    def request_uuid(self): return "collect-teleport-pick-offline"
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

    def count(self, box):
        return sum(inside(box, c) for c in self.clicks)


class TeleportPickTests(unittest.TestCase):
    # ---- base（安卓 / PlayCover）----
    def test_gray_first_is_never_clicked(self):
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_inactive_first.png"), [DESERT_WHITE])
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.count(DESERT_GRAY), 0)
        self.assertEqual(ctrl.count(DESERT_WHITE), 1)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def test_stray_gray_pixel_no_longer_misjudged(self):
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_stray_pixel.png"), [STEIN_WHITE])
        self.assertFalse(timed_out)
        self.assertNotIn(PURPLE, names)
        self.assertEqual(ctrl.count(STEIN_WHITE), 1)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def test_leftmost_white_beats_purple(self):
        working = [RUIN_PURPLE, RUIN_WHITE1, RUIN_WHITE2]
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_purple_two_white.png"), working)
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.count(RUIN_WHITE1), 1)
        self.assertEqual(ctrl.count(RUIN_PURPLE), 0)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def test_dead_first_white_switches_to_second_white(self):
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_purple_two_white.png"), [RUIN_WHITE2])
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.count(RUIN_WHITE1), CLICKS_PER_ICON)
        self.assertEqual(ctrl.count(RUIN_WHITE2), 1)
        self.assertEqual(ctrl.count(RUIN_PURPLE), 0)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def test_dead_whites_fall_back_to_purple(self):
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_purple_two_white.png"), [RUIN_PURPLE])
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.count(RUIN_WHITE1), CLICKS_PER_ICON)
        self.assertEqual(ctrl.count(RUIN_WHITE2), CLICKS_PER_ICON)
        self.assertEqual(ctrl.count(RUIN_PURPLE), 1)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def test_all_dead_gives_up_the_page(self):
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_purple_two_white.png"), [])
        self.assertFalse(timed_out)
        for box in (RUIN_WHITE1, RUIN_WHITE2, RUIN_PURPLE):
            self.assertEqual(ctrl.count(box), CLICKS_PER_ICON)
        self.assertEqual(len(ctrl.clicks), 3 * CLICKS_PER_ICON)
        self.assertIn(GIVE_UP, names)
        self.assertEqual(names[-1], END)

    def test_hit_counts_reset_on_next_page(self):
        # 放弃本页后再进一次入口（模拟翻到下一页）：计数若没清零，第二页的阵一个都不会点
        next_page = {END: {"max_hit": 1, "next": [ENTRY]}}
        ctrl, names, timed_out = self.run_entry(BASE, load("area_map_purple_two_white.png"), [], next_page)
        self.assertFalse(timed_out)
        self.assertEqual(names.count(ENTRY), 2)
        for box in (RUIN_WHITE1, RUIN_WHITE2, RUIN_PURPLE):
            self.assertEqual(ctrl.count(box), 2 * CLICKS_PER_ICON)

    def test_gray_only_gives_up_without_clicking(self):
        ctrl, names, timed_out = self.run_entry(BASE, desert_gray_only(), [DESERT_GRAY])
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.clicks, [])
        self.assertEqual(names[-2:], [GIVE_UP, END])

    # ---- PC（base + pc）：色核参数与 base 共用，只换模板 ----
    def test_pc_gray_only_gives_up_without_clicking(self):
        ctrl, names, timed_out = self.run_entry(PC, load("pc_area_map_two_gray.png"), PC_GRAYS)
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.clicks, [])
        self.assertEqual(names[-2:], [GIVE_UP, END])

    def test_pc_white_beats_purple(self):
        ctrl, names, timed_out = self.run_entry(PC, load("pc_area_map_white_purple.png"), [PC_WHITE, PC_PURPLE])
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.count(PC_WHITE), 1)
        self.assertEqual(ctrl.count(PC_PURPLE), 0)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def test_pc_dead_white_falls_back_to_purple(self):
        ctrl, names, timed_out = self.run_entry(PC, load("pc_area_map_white_purple.png"), [PC_PURPLE])
        self.assertFalse(timed_out)
        self.assertEqual(ctrl.count(PC_WHITE), CLICKS_PER_ICON)
        self.assertEqual(ctrl.count(PC_PURPLE), 1)
        self.assertEqual(names[-1], SKILL_ENTRY)

    def run_entry(self, bundles, frame, working, extra=None):
        no_wait = {"post_delay": 0, "rate_limit": 0}
        # 源码仓库没有 OCR 模型：依赖标题 OCR 的放弃 / 结束节点换成右换页箭头模板，同样代表「仍在区域地图上」
        on_area_map = {"recognition": "Or", "any_of": ["Rec_AreaMap_ButtRight_Tmp"]}
        fixture: dict[str, dict] = {name: dict(no_wait) for name in CLICKERS}
        fixture.update({
            # 内存截图没有箱庭 UI，用右下角白块代表「已到达」
            SKILL_ENTRY: {"recognition": "ColorMatch", "roi": ARRIVED_MARK, "lower": [250, 250, 250],
                          "upper": [255, 255, 255], "count": ARRIVED_MARK[2] * ARRIVED_MARK[3], "next": []},
            GIVE_UP: {**on_area_map, **no_wait},
            END: {**on_area_map, "action": "DoNothing", "next": [], "post_delay": 0},
        })
        for name, override in (extra or {}).items():
            fixture[name] = {**fixture.get(name, {}), **override}
        # MaaFramework keeps its log handle open until process exit on Windows.
        with tempfile.TemporaryDirectory(prefix="mfabd2-teleport-", ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(directory)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            resource = Resource()
            controller = AreaMapController(frame, working)
            tasker = Tasker()
            # 点不动的图标若被无界重点，看门狗停掉后交给断言报错
            fired = threading.Event()
            watchdog = threading.Timer(WATCHDOG_S, lambda: (fired.set(), tasker.post_stop()))
            try:
                for bundle in bundles:
                    self.assertTrue(resource.post_bundle(ROOT / "assets/resource" / bundle).wait().succeeded)
                self.assertTrue(resource.override_pipeline(fixture))
                self.assertTrue(resource.register_custom_action("ClearNodeHitCount",
                                                                clear_hit.ClearNodeHitCountAction()))
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
