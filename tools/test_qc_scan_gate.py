"""Offline checks for the per-run category scan gate (2026-10-07 本地改动).

背景(见 LOCAL_CHANGES.md)：Collect_FeatureSwitch_StoryPack / _CharacterPack /
_EvenPack 上游写死 max_hit:1 —— 一个 task 内每个分类**只能进入一次**。那唯一一次
若被空跑浪费(停止任务时脚本正在进卡带、重跑后残留载入态，分支空等 15s 就退栈)，
本轮该类再也进不去，调度器只能落到还没用过的活动分支(实测 10:46:49 直接去活动)。

修法三部分，本测试各锁一层：
  1. 静态接线：三个分支 max_hit==3 + CheckTag(QC_*_Scanned)；触底出口
     (Collect_LocatePack_SimpleSkip_*) 挂上 Mark 节点；Mark 节点必须带
     「快速卡带列表开着」前置判据(否则 2026-10-07 那个「列表没开→空滑→误判触底」
     的 bug 会升级成整类跳过)；Reset 节点挂在两个任务入口 next 首位。
  2. 状态机：用 agent/recognition/counter.py 的**真实** CheckTag/UpdateTag/ResetTag
     跑准入判定，回归四个场景。
  3. max_hit 封顶：第 4 次不再进入(防死循环)。

跑法(仓库根):  python -m unittest tools.test_qc_scan_gate
"""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = "assets/resource/base/pipeline/Collect_Launcher.json"

# (分支节点, 本轮扫描标记, 触底出口, 置位节点)
BRANCHES = [
    ("Collect_FeatureSwitch_StoryPack", "QC_Story_Scanned",
     "Collect_LocatePack_SimpleSkip_Story", "Collect_QC_Scan_Mark_Story"),
    ("Collect_FeatureSwitch_CharacterPack", "QC_Char_Scanned",
     "Collect_LocatePack_SimpleSkip_Character", "Collect_QC_Scan_Mark_Character"),
    ("Collect_FeatureSwitch_EvenPack", "QC_Event_Scanned",
     "Collect_LocatePack_SimpleSkip_Event", "Collect_QC_Scan_Mark_Event"),
]

ENTRIES = ["Collect_StartGame_HomePage", "Collect_StartGame_HomePage_OnlyOnce"]


def load_counter():
    """用 stub 加载 agent/recognition/counter.py(真实 CheckTag/UpdateTag/ResetTag)。"""
    stubs = {}
    for name in ("maa", "maa.custom_recognition", "maa.custom_action",
                 "maa.agent", "maa.agent.agent_server"):
        stubs[name] = types.ModuleType(name)
    stubs["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(
        custom_recognition=lambda _n: (lambda cls: cls),
        custom_action=lambda _n: (lambda cls: cls),
    )

    class _FakeReco:
        AnalyzeArg = types.SimpleNamespace
        AnalyzeResult = types.SimpleNamespace

    class _FakeAction:
        RunArg = types.SimpleNamespace

    stubs["maa.custom_recognition"].CustomRecognition = _FakeReco
    stubs["maa.custom_action"].CustomAction = _FakeAction
    utils = types.ModuleType("utils")
    utils.mfaalog = types.SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    stubs["utils"] = utils
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "counter_under_test", ROOT / "agent/recognition/counter.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["counter_under_test"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("counter_under_test", None)
    return module


def load_launcher():
    with open(ROOT / LAUNCHER, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 静态接线
class StaticWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = load_launcher()

    def test_branch_max_hit_is_three(self):
        """根因修复:每个分类本轮最多可进 3 次(不是上游的 1 次)。"""
        for name, _tag, _out, _mark in BRANCHES:
            with self.subTest(node=name):
                self.assertEqual(self.d[name].get("max_hit"), 3)

    def test_branch_has_scanned_gate(self):
        """每个分支带 CheckTag(本轮已扫过则不再命中)，tag 名一对一。"""
        for name, tag, _out, _mark in BRANCHES:
            with self.subTest(node=name):
                node = self.d[name]
                self.assertEqual(node.get("recognition"), "Custom")
                self.assertEqual(node.get("custom_recognition"), "CheckTag")
                param = node["custom_recognition_param"]
                self.assertEqual(param.get("tag"), tag)
                self.assertEqual(param.get("max"), 1)

    def test_hit_gate_tags_are_distinct(self):
        """三个 tag 不能重名，否则一类的扫描会封死别类。"""
        tags = [t for _n, t, _o, _m in BRANCHES]
        self.assertEqual(len(tags), len(set(tags)))

    def test_touchdown_exits_point_at_mark_nodes(self):
        """触底出口(SmartSwipOut 锚的目标)必须挂着置位节点，否则标记永不写入。"""
        for _name, _tag, out, mark in BRANCHES:
            with self.subTest(node=out):
                self.assertIn(mark, self.d[out].get("next", []))

    def test_mark_nodes_require_quickcart_open(self):
        """★关键护栏:置位必须有「快速卡带列表开着」前置判据。

        2026-10-07 实测过「复位链退栈到定位链、人在箱庭而列表没开」时 SmartSwipe
        会在箱庭画面上误判触底。若此时也打标记 ⇒ 整类跳过 ⇒ 漏采。
        """
        for _name, tag, _out, mark in BRANCHES:
            with self.subTest(node=mark):
                node = self.d[mark]
                self.assertEqual(node.get("recognition"), "Or")
                self.assertIn("Collect_QuickCart_Menu", node.get("any_of", []))
                self.assertEqual(node.get("custom_action"), "UpdateTag")
                self.assertEqual(node["custom_action_param"].get("tag"), tag)
                self.assertEqual(node["custom_action_param"].get("value"), 1)

    def test_reset_node_shape(self):
        """清零节点:三个 tag 齐全 + max_hit 1(一轮只清一次，不洗掉新打的标记)。"""
        node = self.d["Collect_QC_Scan_Reset"]
        self.assertEqual(node.get("custom_action"), "ResetTag")
        self.assertEqual(sorted(node["custom_action_param"]["tags"]),
                         sorted(t for _n, t, _o, _m in BRANCHES))
        self.assertEqual(node.get("max_hit"), 1)

    def test_reset_hooked_on_both_entries(self):
        """两个任务入口(完整 / 单章)都必须清零 —— TAG_STORE 不随停止任务重置。"""
        for entry in ENTRIES:
            with self.subTest(entry=entry):
                nxt = self.d[entry].get("next", [])
                self.assertEqual(nxt[0], "[JumpBack]Collect_QC_Scan_Reset")

    def test_reset_also_on_sandplay(self):
        self.assertIn("[JumpBack]Collect_QC_Scan_Reset",
                      self.d["Collect_Sandplay"].get("next", []))

    # ---------------------------------------------------- 2026-10-09 分类路由统一
    def test_skip_gates_stay_plain_jumps(self):
        """★回归 11:54 死循环:两个分类闸必须保持**普通跳转**，绝不能改 [JumpBack]。

        改 JumpBack 后，为了让"整类已完成"不再被重扫而挂的 [JumpBack]Collect_QC_Scan_Mark_*
        会被反复求值(Mark 判据 Or[Collect_QuickCart_Menu] 恒命中 + jump_back 返回后
        MaaFW 重跑整个 next 列表) ⇒ QC_Story_Scanned 一路 +1 到 12+，任务卡死。
        """
        nxt = self.d["Collect_QuickCart_Menu"]["next"]
        for gate in ("Collect_QC_Skip_Story", "Collect_QC_Skip_Character"):
            with self.subTest(gate=gate):
                self.assertIn(gate, nxt)
                self.assertNotIn(f"[JumpBack]{gate}", nxt)

    def test_no_always_hit_node_as_jumpback_target(self):
        """恒命中节点(DirectHit / Or[Collect_QuickCart_Menu])不能当 [JumpBack] 目标。

        它们是 11:54 死循环的直接成因:MaaFW 在 jump_back 返回后会重跑父节点整个 next
        列表，恒命中 ⇒ 无限执行(且 UpdateTag 的 value 是**累加**，tag 会失控)。
        """
        for name, node in self.d.items():
            for item in (node.get("next") or []):
                if not item.startswith("[JumpBack]"):
                    continue
                target = self.d.get(item[len("[JumpBack]"):])
                if not isinstance(target, dict):
                    continue
                with self.subTest(parent=name, target=item):
                    if target.get("recognition") == "DirectHit":
                        self.fail(f"{item} 是 DirectHit，当 JumpBack 目标会死循环")
                    if target.get("recognition") == "Or":
                        self.assertNotIn("Collect_QuickCart_Menu", target.get("any_of", []),
                                         f"{item} 在列表开着时恒命中，当 JumpBack 目标会死循环")

    def test_dispatcher_has_every_branch(self):
        """三类分支都要在调度器里，顺序:跳过闸在对应分支之前。"""
        nxt = [x.replace("[JumpBack]", "") for x in self.d["Collect_QuickCart_Menu"]["next"]]
        for gate, branch in (("Collect_QC_Skip_Story", "Collect_FeatureSwitch_StoryPack"),
                             ("Collect_QC_Skip_Character", "Collect_FeatureSwitch_CharacterPack")):
            with self.subTest(gate=gate):
                self.assertLess(nxt.index(gate), nxt.index(branch))
        for _name, _tag, _out, _mark in BRANCHES:
            self.assertIn(_name, nxt)

    def test_pending_chain_has_escape_routes(self):
        """★根因修复:Pending 节点必须有「本类扫完 → 下一类 / 收工」的出口。

        原来只有 [JumpBack]FeatureSwitch_<类>Pack 一项，本类扫完被 CheckTag 挡住后
        无处可去 ⇒ 空转 20s ⇒ on_error 落 Global_Null ⇒ 任务假成功收尾(09:58 漏采活动类)。
        """
        self.assertEqual(
            self.d["Collect_QC_Char_Pending"]["next"],
            ["[JumpBack]Collect_FeatureSwitch_CharacterPack",
             "Collect_QC_Event_Pending", "Collect_QC_All_Done"])
        self.assertEqual(
            self.d["Collect_QC_Event_Pending"]["next"],
            ["[JumpBack]Collect_FeatureSwitch_EvenPack", "Collect_QC_All_Done"])

    def test_force_list_patches_pending_entries(self):
        """强采必须打通类入口:角色/活动各有 Pending 节点，判据只看存档、与强采清单无关。

        不补这一环就会出现"设了强采角色卡3却进不去角色分支"(实测 11:22)。
        剧情类没有 Pending 节点，靠 ForceListApply 关 Collect_QC_Skip_Story 走调度器路径。
        """
        src = (ROOT / "agent/action/string_processor.py").read_text(encoding="utf-8")
        self.assertIn('"CharacterPack": "Collect_QC_Char_Pending"', src)
        self.assertIn('"EventPack": "Collect_QC_Event_Pending"', src)
        for name in ("Collect_QC_Char_Pending", "Collect_QC_Event_Pending"):
            self.assertIn(name, self.d, f"{name} 必须存在(强采补丁的目标节点)")


# ------------------------------------------------------------------ 状态机
class GateStateMachineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = load_counter()
        cls.d = load_launcher()

    def setUp(self):
        # 每例清空真实 TAG_STORE，避免用例互相污染
        self.c.TAG_STORE.clear()
        self.hits = {}

    def _check_tag(self, tag, max_val):
        reco = self.c.CheckTag()
        argv = types.SimpleNamespace(
            custom_recognition_param=json.dumps({"tag": tag, "max": max_val}))
        return reco.analyze(None, argv) is not None

    def _update_tag(self, tag, value=1):
        act = self.c.UpdateTag()
        argv = types.SimpleNamespace(
            custom_action_param=json.dumps({"tag": tag, "value": value}))
        return act.run(None, argv)

    def _reset_all(self):
        act = self.c.ResetTag()
        argv = types.SimpleNamespace(
            custom_action_param=json.dumps({"tags": [t for _n, t, _o, _m in BRANCHES]}))
        return act.run(None, argv)

    def _admit(self, node, tag):
        """复刻框架语义:候选 = 未到 max_hit 且 recognition 命中。"""
        max_hit = self.d[node]["max_hit"]
        if self.hits.get(node, 0) >= max_hit:
            return False
        ok = self._check_tag(tag, 1)
        if ok:
            self.hits[node] = self.hits.get(node, 0) + 1
        return ok

    def _touchdown(self, mark_node, tag, quickcart_open):
        """触底:列表开着才置位(Or[Collect_QuickCart_Menu] 前置判据)。"""
        if not quickcart_open:
            return False
        return self._update_tag(tag)

    def test_first_round_all_branches_admitted(self):
        """首轮三类标记都是 0 ⇒ 剧情分支可进(修复前空跑一次就再也进不去)。"""
        for node, tag, _out, _mark in BRANCHES:
            with self.subTest(node=node):
                self.assertTrue(self._admit(node, tag))

    def test_after_real_touchdown_story_not_reentered(self):
        """剧情正常扫完一轮(列表开着触底)⇒ 置位 ⇒ 不再进入，落到下一类。"""
        node, tag, _out, mark = BRANCHES[0]
        self.assertTrue(self._admit(node, tag))
        self.assertTrue(self._touchdown(mark, tag, quickcart_open=True))
        self.assertFalse(self._admit(node, tag))
        # 角色/活动不受剧情标记影响
        self.assertTrue(self._admit(BRANCHES[1][0], BRANCHES[1][1]))

    def test_bogus_touchdown_does_not_lock_the_class(self):
        """★列表没开的误触底(2026-10-07 已知 bug)不打标记 ⇒ 本类仍可进入。"""
        node, tag, _out, mark = BRANCHES[0]
        self.assertTrue(self._admit(node, tag))
        self.assertFalse(self._touchdown(mark, tag, quickcart_open=False))
        self.assertTrue(self._admit(node, tag), "误触底不应封死本类")

    def test_new_round_reset_clears_marks(self):
        """新一轮入口 Reset 后标记归零 ⇒ 剧情重新可进(停止重跑场景)。"""
        node, tag, _out, mark = BRANCHES[0]
        self.assertTrue(self._admit(node, tag))
        self.assertTrue(self._touchdown(mark, tag, quickcart_open=True))
        self.assertFalse(self._admit(node, tag))
        self._reset_all()
        self.assertTrue(self._admit(node, tag))

    def test_max_hit_caps_at_three(self):
        """就算一直没触底，同一类最多进 3 次 ⇒ 不会无限重扫(防死循环)。"""
        node, tag, _out, _mark = BRANCHES[0]
        self.assertEqual([self._admit(node, tag) for _ in range(4)],
                         [True, True, True, False])


if __name__ == "__main__":
    unittest.main()
