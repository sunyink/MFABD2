"""吸取技能组按栏位分流的回归：真实资源与内核，内存截图，不连接游戏。

起因（2026-10-19）：同一卡带当周中断后重跑，已吸过的点没有拾取物，点吸取只弹提示、计数不涨，
但吸取图标的可用色核仍亮，原 Collect_Skill3_2 的 next 自环无界，任务永不结束（沙漠之花 tp1
实测 156 连点，计数一直 8/10）。之后技能组改成每个栏位一个分流节点（Collect_Skill3_N_Start）：

- 施放节点点完回到本栏位的分流节点重新判，一次调用最多点 6 次（施放节点的 max_hit），
  Collect_Skill_Tap3_Enable 每次进模块先用 ClearNodeHitCount 把计数清零；
- 分流节点一帧都认不出时，等 timeout 后经 on_error 下移到下一栏；失败提示写在分流节点自己的 focus 上；
- 召集、压制次数用满后打开旁路节点，本 task 后续调用整组跳过，task 结束补丁自然失效。

夹具 skill_bar_no_target_toast.png 取自 2026-10-19 实机截图，只保留吸取图标、第 3 页图钉与提示框
三块，其余填平色。源码仓库没有 OCR 模型，认提示的用例在模型缺失时跳过；把装好的
assets/resource/base/model/ocr 复制过来即可跑全。

运行：python -B tools/verify_collect_absorb_no_target.py -v
"""

from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from maa.context import ContextEventSink
from maa.controller import CustomController
from maa.define import LoggingLevelEnum
from maa.event_sink import NotificationType
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/resource/base"
AGENT = ROOT / "agent"
FIXTURE = Path(__file__).resolve().parent / "fixtures/collect_absorb_no_target/skill_bar_no_target_toast.png"
Library.version()
from maa.agent.agent_server import AgentServer


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _package(name, **children):
    package = types.ModuleType(name)
    for child, module in children.items():
        setattr(package, child, module)
    sys.modules[name] = package


# Native tests register callbacks on Resource, not on an AgentServer process.
# 只加载用到的几个文件，不经 utils / recognition 的 __init__ 拉起整个 agent
with patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_action", return_value=lambda cls: cls):
    _package("utils", mfaalog=_load("utils.mfaalog", AGENT / "utils/mfaalog.py"))
    _package("recognition", counter=_load("recognition.counter", AGENT / "recognition/counter.py"))
    clear_node_hit_count = _load("clear_node_hit_count", AGENT / "action/clear_node_hit_count.py")
    pipeline_manager = _load("pipeline_manager", AGENT / "action/pipeline_manager.py")

ENTRY = "Collect_Skill_Tap3_Enable"
ABSORB_START = "Collect_Skill3_2_Start"
ABSORB = "Collect_Skill3_2"
USED = "Collect_Skill3_2_Used"
NO_TARGET = "Collect_Skill3_2_NoTarget"
SUMMON_START = "Collect_Skill3_3_Start"
SUMMON_DEPLETED = "Collect_Skill3_3_perOcr"
SUMMON_BYPASS = "Collect_Skill3_3_Bypass"
SUPPRESS_START = "Collect_Skill3_5_Start"
DISPATCHERS = ["Collect_Skill3_1_Start", ABSORB_START, SUMMON_START, SUPPRESS_START]
SKILL_USED = "Collect_OnlyOnce_SkillUsed"
CLICKS_LIMIT = 6
ABSORB_ICON = [933, 526, 75, 73]
ABSORB_DOT = [954, 555, 8, 13]  # 可用色核；吸完后变灰
TOAST = [440, 140, 400, 55]
HAS_OCR = (BASE / "model/ocr/rec.onnx").exists()
WATCHDOG_S = 60

ABSORB_ONLY = {
    ENTRY: {"next": [ABSORB_START]},  # 跳过技能设置、探查，直接到吸取
    ABSORB: {"post_delay": 0},
    SUMMON_START: {"next": [], "on_error": []},  # 走到 3-3 的分流节点就结束
}


def call_twice_after(node):
    """走到 node 时回到 Tap3_Enable 再调用一次技能组，第二次走到就结束。"""
    return {
        node: {"next": ["test_collect_again", "test_collect_end"], "on_error": []},
        "test_collect_again": {"max_hit": 1, "pre_delay": 0, "post_delay": 0, "next": [ENTRY]},
        "test_collect_end": {"pre_delay": 0, "post_delay": 0},
    }


def merged(*layers):
    result = {}
    for layer in layers:
        for name, fields in layer.items():
            result.setdefault(name, {}).update(fields)
    return result


def strip_attr(name):
    return name.removeprefix("[JumpBack]")


def load_frames():
    with_toast = np.ascontiguousarray(np.asarray(Image.open(FIXTURE).convert("RGB"))[:, :, ::-1])
    without_toast = with_toast.copy()
    x, y, w, h = TOAST
    without_toast[y:y + h, x:x + w] = (64, 60, 60)
    used = without_toast.copy()
    x, y, w, h = ABSORB_DOT
    used[y:y + h, x:x + w] = 148  # Collect_Skill3_2_Used 的灰色色核
    return with_toast, without_toast, used


class SkillBarController(CustomController):
    """技能栏画面：点吸取之前一律是 before，点过之后换成 after。"""

    def __init__(self, before, after):
        super().__init__()
        self.before = before
        self.after = after
        self.clicks = []

    def connect(self): return True
    def request_uuid(self): return "collect-absorb-no-target-offline"
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

    def absorb_clicks(self):
        x, y, w, h = ABSORB_ICON
        return sum(x <= cx < x + w and y <= cy < y + h for cx, cy in self.clicks)

    def screencap(self):
        return self.after if self.absorb_clicks() else self.before


class FailedNodes(ContextEventSink):
    """收集 Node.PipelineNode.Failed：事件带的是哪个节点的名字和 focus。"""

    def __init__(self):
        super().__init__()
        self.items = []

    def on_node_pipeline_node(self, context, noti_type, detail):
        if noti_type == NotificationType.Failed:
            self.items.append((detail.name, detail.focus))


class SkillSlotTests(unittest.TestCase):
    def test_dispatcher_structure(self):
        skills = json.loads((BASE / "pipeline/Collect_skills.json").read_text(encoding="utf-8"))
        launcher = json.loads((BASE / "pipeline/Collect_Launcher.json").read_text(encoding="utf-8"))
        cleared = skills[ENTRY]["custom_action_param"]["node_name"]
        for name in DISPATCHERS:
            with self.subTest(name):
                node = skills[name]
                candidates = [strip_attr(n) for n in node["next"]]
                fallthrough = [strip_attr(n) for n in node["on_error"]]
                self.assertIn("timeout", node)
                self.assertIn("Node.PipelineNode.Failed", node["focus"])
                self.assertFalse(set(candidates) & set(fallthrough))
                # on_error 列表沿用分流节点的短 timeout，落点认不到整个 task 会失败，所以必须必中
                for target in fallthrough:
                    self.assertNotIn("recognition", skills[target])
                    self.assertNotEqual(skills[target].get("enabled"), False)
                casts = [n for n in candidates if skills[n].get("action") == "Click"]
                self.assertEqual(len(casts), 1)
                cast = casts[0]
                self.assertEqual(skills[cast]["max_hit"], CLICKS_LIMIT)
                self.assertIn(cast, cleared)
                self.assertIn(name, [strip_attr(n) for n in skills[cast]["next"]])
                for n in candidates:
                    if n.endswith("_Bypass"):
                        self.assertEqual(candidates[0], n)
                        self.assertIs(skills[n]["enabled"], False)
                    if n.endswith("_Skip"):
                        self.assertGreater(candidates.index(n), candidates.index(cast))
        patches = launcher["Collect_OnlyOnce_Switch"]["custom_action_param"]["patches"]
        self.assertEqual(skills[ABSORB]["next"], [NO_TARGET, ABSORB_START])
        self.assertEqual(patches[ABSORB]["next"], [NO_TARGET, f"[JumpBack]{SKILL_USED}", ABSORB_START])
        self.assertEqual(patches[NO_TARGET]["next"], [f"[JumpBack]{SKILL_USED}", *skills[NO_TARGET]["next"]])

    @unittest.skipUnless(HAS_OCR, "源码仓库没有 OCR 模型")
    def test_no_target_toast_skips_absorb(self):
        with_toast, without_toast, _ = load_frames()
        (controller, names, timed_out, failed), = self.run_tasks(
            ABSORB_ONLY, SkillBarController(without_toast, with_toast))
        self.assertFalse(timed_out)
        self.assertEqual(controller.absorb_clicks(), 1)
        self.assertEqual(names[-2:], [NO_TARGET, SUMMON_START])
        self.assertEqual(failed, [])

    def test_unresponsive_absorb_stops_at_max_hit(self):
        _, without_toast, _ = load_frames()
        (controller, names, timed_out, failed), = self.run_tasks(
            ABSORB_ONLY, SkillBarController(without_toast, without_toast))
        self.assertFalse(timed_out)
        self.assertEqual(controller.absorb_clicks(), CLICKS_LIMIT)
        self.assertNotIn(NO_TARGET, names)
        self.assertEqual(names[-2:], [ABSORB_START, SUMMON_START])
        # 超时的失败事件用的是分流节点自己的名字和 focus，失败提示写在分流节点上即可，不需要单独的落点节点
        self.assertEqual([name for name, _ in failed], [ABSORB_START])
        self.assertIn("Node.PipelineNode.Failed", failed[0][1])

    def test_click_budget_resets_each_call(self):
        _, without_toast, _ = load_frames()
        (controller, names, timed_out, _), = self.run_tasks(
            merged(ABSORB_ONLY, call_twice_after(SUMMON_START)), SkillBarController(without_toast, without_toast))
        self.assertFalse(timed_out)
        self.assertEqual(names.count(ENTRY), 2)
        # max_hit 在整个 task 里累计；不清零的话第二次调用一下都点不了
        self.assertEqual(controller.absorb_clicks(), 2 * CLICKS_LIMIT)

    def test_working_absorb_clicks_once(self):
        _, without_toast, used = load_frames()
        (controller, names, timed_out, failed), = self.run_tasks(
            ABSORB_ONLY, SkillBarController(without_toast, used))
        self.assertFalse(timed_out)
        self.assertEqual(controller.absorb_clicks(), 1)
        self.assertEqual(names[-3:], [ABSORB_START, USED, SUMMON_START])
        self.assertEqual(failed, [])

    def test_unknown_slots_fall_through_to_exit(self):
        # 夹具里探查、召集、压制三块都是平色：三个分流节点各自超时下移，吸取照常放，最后走到出口
        _, without_toast, used = load_frames()
        overrides = {
            ENTRY: {"next": ["Collect_Skill3_1_Start"]},
            ABSORB: {"post_delay": 0},
            "Collect_Skill_TapReset": {"recognition": "DirectHit", "next": []},
        }
        (controller, names, timed_out, failed), = self.run_tasks(overrides, SkillBarController(without_toast, used))
        self.assertFalse(timed_out)
        self.assertEqual(controller.absorb_clicks(), 1)
        self.assertEqual([name for name, _ in failed], ["Collect_Skill3_1_Start", SUMMON_START, SUPPRESS_START])
        self.assertEqual(names[-2:], ["Collect_Skill3_5_Pass", "Collect_Skill_TapReset"])

    def test_scan_success_disables_nothing_candidates(self):
        # 吸取栏的「未设置」改成必中来模拟空栏位：检查通过的 task 里它不再参与，没走检查时照常命中
        _, without_toast, used = load_frames()
        nothing = "Collect_Skill3_2_Nothing"
        scan = {
            ENTRY: {"next": ["[JumpBack]Setup_SkillScan_Success", ABSORB_START]},
            "Setup_SkillScan_Success": {"action": "DoNothing", "max_hit": 1},  # 不测检查本身与打卡
        }
        for label, layer, expect_nothing in (("scanned", scan, False), ("not scanned", {}, True)):
            with self.subTest(label):
                overrides = merged(ABSORB_ONLY, {nothing: {"recognition": "DirectHit"}}, layer)
                (controller, names, timed_out, _), = self.run_tasks(overrides, SkillBarController(without_toast, used))
                self.assertFalse(timed_out)
                self.assertEqual(nothing in names, expect_nothing)
                self.assertEqual(controller.absorb_clicks(), 0 if expect_nothing else 1)

    def test_depleted_slot_bypassed_until_task_ends(self):
        _, without_toast, _ = load_frames()
        overrides = merged(
            {ENTRY: {"next": [SUMMON_START]},
             SUMMON_DEPLETED: {"recognition": "DirectHit"}},  # 不测 OCR，只测「用满 → 打开旁路」的接线
            call_twice_after(SUPPRESS_START),
        )
        # 同一个 Resource 上跑两个 task：旁路只在打开它的那个 task 里生效
        runs = self.run_tasks(overrides, *(SkillBarController(without_toast, without_toast) for _ in range(2)))
        for controller, names, timed_out, _ in runs:
            self.assertFalse(timed_out)
            self.assertEqual(names.count(ENTRY), 2)
            self.assertEqual(names.count(SUMMON_DEPLETED), 1)
            self.assertEqual(names.count(SUMMON_BYPASS), 1)
            self.assertLess(names.index(SUMMON_DEPLETED), names.index(SUMMON_BYPASS))

    def test_onlyonce_marks_absorb_once(self):
        launcher = json.loads((BASE / "pipeline/Collect_Launcher.json").read_text(encoding="utf-8"))
        patches = launcher["Collect_OnlyOnce_Switch"]["custom_action_param"]["patches"]
        with_toast, without_toast, _ = load_frames()
        cases = [("unresponsive", without_toast, CLICKS_LIMIT)]
        if HAS_OCR:
            cases.append(("toast", with_toast, 1))
        for label, after, clicks in cases:
            with self.subTest(label):
                (controller, names, timed_out, _), = self.run_tasks(
                    merged(ABSORB_ONLY, patches), SkillBarController(without_toast, after))
                self.assertFalse(timed_out)
                self.assertEqual(controller.absorb_clicks(), clicks)
                self.assertEqual(names.count(SKILL_USED), 1)
                self.assertEqual(names[-1], SUMMON_START)

    def run_tasks(self, overrides, *controllers):
        """同一 Resource 上依次跑若干个 task，每个都从 Tap3_Enable 进入。"""
        results = []
        # MaaFramework keeps its log handle open until process exit on Windows.
        with tempfile.TemporaryDirectory(prefix="mfabd2-skills-", ignore_cleanup_errors=True) as directory:
            Tasker.set_log_dir(directory)
            Tasker.set_stdout_level(LoggingLevelEnum.Off)
            Tasker.set_save_draw(False)
            Tasker.set_save_on_error(False)
            resource = Resource()
            try:
                self.assertTrue(resource.post_bundle(BASE).wait().succeeded)
                self.assertTrue(resource.override_pipeline(overrides))
                self.assertTrue(resource.register_custom_action(
                    "ClearNodeHitCount", clear_node_hit_count.ClearNodeHitCountAction()))
                self.assertTrue(resource.register_custom_action("PatchPipeline", pipeline_manager.PatchPipeline()))
                for controller in controllers:
                    results.append(self.run_once(resource, controller))
                return results
            finally:
                resource.clear()
                Tasker.set_log_dir("")

    def run_once(self, resource, controller):
        tasker = Tasker()
        failed = FailedNodes()
        tasker.add_context_sink(failed)
        # 回环没被限住时会一直点，看门狗停掉后交给断言报错
        fired = threading.Event()
        watchdog = threading.Timer(WATCHDOG_S, lambda: (fired.set(), tasker.post_stop()))
        try:
            self.assertTrue(controller.post_connection().wait().succeeded)
            self.assertTrue(tasker.bind(resource, controller))
            watchdog.start()
            detail = tasker.post_task(ENTRY).wait().get()
            self.assertIsNotNone(detail)
            assert detail
            names =[node.name for node in detail.nodes if node and node.name]
            return controller, names, fired.is_set(), failed.items
        finally:
            watchdog.cancel()
            tasker.post_stop().wait()
            del tasker


if __name__ == "__main__":
    unittest.main()
