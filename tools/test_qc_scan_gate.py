"""Offline checks for the per-run category scan gate and probe completion routing.

背景：Collect_FeatureSwitch_StoryPack / _CharacterPack /
_EvenPack 上游写死 max_hit:1 —— 一个 task 内每个分类**只能进入一次**。那唯一一次
若被空跑浪费(停止任务时脚本正在进卡带、重跑后残留载入态，分支空等 15s 就退栈)，
本轮该类再也进不去，调度器只能落到还没用过的活动分支(实测 10:46:49 直接去活动)。

修法三部分，本测试各锁一层：
  1. 静态接线：三个分支 max_hit==3 + CheckTag(QC_*_Scanned)；触底和滑动上限
     共用 PackScan_End，只更新本轮标记，不写完成存档；Mark 节点必须带
     「快速卡带列表开着」前置判据(否则 2026-10-07 那个「列表没开→空滑→误判触底」
     的 bug 会升级成整类跳过)；两个任务入口先检查通信，Reset 节点挂在采集流程首位
     (每周模式先经 Collect_OnlyOnce_Switch 检查本周期)。
  2. 状态机：用 agent/recognition/counter.py 的**真实** CheckTag/UpdateTag/ResetTag
     跑准入判定，回归四个场景。
  3. max_hit 封顶：第 4 次不再进入(防死循环)。
  4. 2026-10-11 名单维护链 + 目标选择器：调度器只执行；排除 → 自选采集范围(全局) / 调试直采 汇到
     Collect_PackType_Select，它按「运行态启用的具名卡带 − 本周已完成」关掉没活的类别入口。
  5. 探针入口先初始化本类收尾，具名卡身份读成功载荷；每周一次在成功打标并禁用后停止。

跑法(仓库根):  python -m unittest tools.test_qc_scan_gate
"""

import importlib.util
import copy
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = "assets/resource/base/pipeline/Collect_Launcher.json"

# (分支节点, 本轮扫描标记, 分类准备节点, 置位节点)
BRANCHES = [
    ("Collect_FeatureSwitch_StoryPack", "QC_Story_Scanned",
     "Collect_StoryPack_PrepareScan", "Collect_QC_Scan_Mark_Story"),
    ("Collect_FeatureSwitch_CharacterPack", "QC_Char_Scanned",
     "Collect_CharacterPack_PrepareScan", "Collect_QC_Scan_Mark_Character"),
    ("Collect_FeatureSwitch_EvenPack", "QC_Event_Scanned",
     "Collect_EventPack_PrepareScan", "Collect_QC_Scan_Mark_Event"),
]

ENTRIES = ["Collect_StartGame_HomePage", "Collect_StartGame_HomePage_OnlyOnce"]
PACK_NAME_RE = re.compile(r"^Collect_Pack_(Story|Character|Event)_\d+$")


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
        for name, _tag, _setup, _mark in BRANCHES:
            with self.subTest(node=name):
                self.assertEqual(self.d[name].get("max_hit"), 3)

    def test_branch_has_scanned_gate(self):
        """每个分支带 CheckTag(本轮已扫过则不再命中)，tag 名一对一。"""
        for name, tag, _setup, _mark in BRANCHES:
            with self.subTest(node=name):
                node = self.d[name]
                self.assertEqual(node.get("recognition"), "Custom")
                self.assertEqual(node.get("custom_recognition"), "CheckTag")
                param = node["custom_recognition_param"]
                self.assertEqual(param.get("tag"), tag)
                self.assertEqual(param.get("max"), 1)

    def test_hit_gate_tags_are_distinct(self):
        """三个 tag 不能重名，否则一类的扫描会封死别类。"""
        tags = [t for _n, t, _s, _m in BRANCHES]
        self.assertEqual(len(tags), len(set(tags)))

    def test_scan_exits_only_mark_current_round(self):
        """触底和达到滑动上限共用本类出口，收尾只更新本轮标记，不写周完成记录。"""
        select_types = [
            "Collect_QuickCart_SelectType_StoryPack",
            "Collect_QuickCart_SelectType_CharacterPack",
            "Collect_QuickCart_SelectType_EventPack",
        ]
        for (name, _tag, setup, mark), select_type in zip(BRANCHES, select_types):
            with self.subTest(node=setup):
                self.assertEqual(self.d[name]["next"], ["[JumpBack]Global_QuickCart_MenuReset", setup])
                self.assertEqual(self.d[name]["anchor"], {"PackScan_End": ""})
                self.assertEqual(self.d[setup]["anchor"], {"PackScan_End": mark})
                self.assertEqual(self.d[setup].get("recognition", "DirectHit"), "DirectHit")
                self.assertNotIn("custom_action", self.d[setup])
                self.assertEqual(self.d[setup]["next"][0], select_type)
                self.assertEqual(self.d[mark]["custom_action"], "UpdateTag")
                self.assertNotIn("next", self.d[mark])
        for exit_node in ("Collect_LocatePack_Swip_VerifyOpen", "Collect_PackScroll_Limit"):
            with self.subTest(node=exit_node):
                self.assertEqual(self.d[exit_node]["next"], ["[Anchor]PackScan_End"])
                self.assertNotIn("custom_action", self.d[exit_node])

    def test_only_once_stops_after_success_and_disable(self):
        """单卡模式仍先写完成记录、禁用当前卡，随后才写每周一次记录并停止。"""
        navigation = json.loads((ROOT / "assets/resource/base/pipeline/Collect_Navigation.json").read_text(encoding="utf-8"))
        patches = self.d["Collect_OnlyOnce_Switch"]["custom_action_param"]["patches"]
        self.assertNotIn("Collect_PackEnd_Ck", patches)
        self.assertEqual(navigation["Collect_PackEnd_Ck"]["next"], ["Collect_PackEnd_Disable"])
        self.assertEqual(patches["Collect_PackEnd_Disable"]["next"], ["Collect_PackEnd_Ck_OnlyOnce"])
        self.assertEqual(navigation["Collect_PackEnd_Ck_OnlyOnce"]["next"], ["Collect_PackEnd_Ck_OnlyOnce_StopTask"])
        self.assertEqual(navigation["Collect_PackEnd_Ck_OnlyOnce_StopTask"]["action"], "StopTask")
        self.assertNotIn("Collect_PackEnd_Failed", patches)

    def test_mark_nodes_require_quickcart_open(self):
        """★关键护栏:置位必须有「快速卡带列表开着」前置判据。

        2026-10-07 实测过「复位链退栈到定位链、人在箱庭而列表没开」时 SmartSwipe
        会在箱庭画面上误判触底。若此时也打标记 ⇒ 整类跳过 ⇒ 漏采。
        """
        for _name, tag, _setup, mark in BRANCHES:
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
                         sorted(t for _n, t, _s, _m in BRANCHES))
        self.assertEqual(node.get("max_hit"), 1)

    def test_reset_hooked_on_both_entries(self):
        """两个任务入口(完整 / 单章)都必须清零 —— TAG_STORE 不随停止任务重置。"""
        for entry in ENTRIES:
            with self.subTest(entry=entry):
                nxt = self.d[entry].get("next", [])
                self.assertEqual(nxt[0], "Env_AgentLost_Stop")
                if entry == "Collect_StartGame_HomePage_OnlyOnce":
                    self.assertEqual(nxt[1:], ["Collect_OnlyOnce_Switch"])
                    nxt = self.d["Collect_OnlyOnce_Switch"].get("next", [])
                else:
                    nxt = nxt[1:]
                self.assertEqual(nxt[:2], ["[JumpBack]Collect_QC_Scan_Reset",
                                           "[JumpBack]Collect_RunTags_Reset"])

    def test_reset_also_on_sandplay(self):
        self.assertIn("[JumpBack]Collect_QC_Scan_Reset",
                      self.d["Collect_Sandplay"].get("next", []))

    # ---------------------------------------------------- 恒命中节点护栏
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

    # ------------------------------------------- 2026-10-11 名单维护链 + 目标选择器
    def test_dispatcher_only_dispatches(self):
        """调度器只管执行:清一色 [JumpBack] 进各类入口，末位普通跳转到出口。

        要不要进某类由目标选择器提前关掉入口决定，调度器里不再挂判据闸(旧 Skip/Pending 双调度已拆除)。
        """
        self.assertEqual(self.d["Collect_QuickCart_Menu"]["next"], [
            "[JumpBack]Collect_QC_RewardSwitch_Off_Click",
            "[JumpBack]Collect_FeatureSwitch_StoryPack",
            "[JumpBack]Collect_FeatureSwitch_CharacterPack",
            "[JumpBack]Collect_FeatureSwitch_EvenPack",
            "[JumpBack]Collect_FeatureSwitch_ManagerPack",
            "Collect_QC_All_Done",
        ])

    def test_exit_node_shape(self):
        """出口无 next、不设 max_hit:调度器在卡带链内被重入时，每一层都要能靠它退栈。"""
        node = self.d["Collect_QC_All_Done"]
        self.assertEqual(node.get("recognition"), "DirectHit")
        self.assertNotIn("next", node)
        self.assertNotIn("max_hit", node)

    def test_list_chain_wiring(self):
        """Sandplay → 调试(默认关) 或 屏蔽 → 指定，两条路都汇到目标选择器，再进调度器。"""
        nxt = self.d["Collect_Sandplay"]["next"]
        self.assertLess(nxt.index("Collect_DebugList_User"), nxt.index("Collect_BlackList_User"))
        self.assertIs(self.d["Collect_DebugList_User"].get("enabled"), False)
        self.assertEqual(self.d["Collect_BlackList_User"]["next"], ["Collect_WhiteList_User"])
        for node in ("Collect_WhiteList_User", "Collect_DebugList_User"):
            with self.subTest(node=node):
                self.assertEqual(self.d[node]["next"], ["Collect_PackType_Select"])
        self.assertEqual(self.d["Collect_PackType_Select"]["next"],
                         ["Collect_QuickCart_Menu", "[JumpBack]Global_QuickCart_MenuReset"])

    def test_selector_types_match_nodes(self):
        """目标选择器的对照表:每类入口都在调度器里，前缀能枚举到具名卡带节点。"""
        types_ = self.d["Collect_PackType_Select"]["custom_action_param"]["types"]
        menu = self.d["Collect_QuickCart_Menu"]["next"]
        for t in types_:
            with self.subTest(type=t["label"]):
                self.assertIn(f"[JumpBack]{t['switch']}", menu)
                prefix = t["pack_prefix"]
                self.assertTrue(any(k.startswith(prefix) and k[len(prefix):].isdigit() for k in self.d))
        self.assertEqual({t["switch"] for t in types_}, {n for n, _t, _s, _m in BRANCHES})

    def test_old_gate_chain_removed(self):
        for name in ("Collect_QC_Skip_Story", "Collect_QC_Skip_Character",
                     "Collect_QC_Char_Pending", "Collect_QC_Event_Pending",
                     "Collect_ForceList_Apply"):
            with self.subTest(node=name):
                self.assertNotIn(name, self.d)


# ------------------------------------------------------------------ 目标选择器
def load_selector(done_cards, sync_ok=True):
    """用 stub 加载 agent/action/pack_type_select.py。

    done_cards: 本周已完成的卡名集合，喂给假的 CooldownManager。
    调试名单读取使用真实 utils.collect_config；存档与账号同步替身均不接触本机数据。
    """
    stubs = {}
    for name in ("maa", "maa.agent", "maa.agent.agent_server", "maa.context", "maa.custom_action",
                 "utils", "utils.account_sync",
                 "action", "action.cartridge_lib"):
        stubs[name] = types.ModuleType(name)
    stubs["action"].__path__ = []
    stubs["utils"].__path__ = [str(ROOT / "agent/utils")]
    stubs["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(
        custom_action=lambda _n: (lambda cls: cls))
    stubs["maa.context"].Context = object

    class _FakeAction:
        RunArg = types.SimpleNamespace
        RunResult = types.SimpleNamespace

    stubs["maa.custom_action"].CustomAction = _FakeAction
    stubs["utils"].mfaalog = types.SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    stubs["utils.account_sync"].sync_from_context = lambda _c, where="": sync_ok

    def check_availability(params):
        return all(t["card_name"] in done_cards for t in params["targets"])

    stubs["action.cartridge_lib"].manager = types.SimpleNamespace(check_availability=check_availability)

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "action.pack_type_select", ROOT / "agent/action/pack_type_select.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["action.pack_type_select"] = module
        spec.loader.exec_module(module)
    return module


class FakeContext:
    """get_node_data 返回与 MaaFw 5.12.2 实测一致的 V2 归一化结构(探针见 priv _Work/2026-10-11-pr543-overview)。"""

    def __init__(self, launcher, disabled=(), debug_map=None, broken=(), attach=None):
        self.launcher = launcher
        self.disabled = set(disabled)
        self.broken = set(broken)
        self.debug_enabled = debug_map is not None
        self.attach = dict(attach or {})
        if debug_map is not None:
            self.attach.setdefault("Collect_DebugList_User", {
                key: ",".join(str(n) for n in sorted(numbers)) for key, numbers in debug_map.items()
            })
        self.patches = {}
        self.tasker = types.SimpleNamespace(resource=types.SimpleNamespace(node_list=list(launcher)))

    def get_node_object(self, name):
        return types.SimpleNamespace(attach=self.attach.get(name, {}))

    def get_node_data(self, name):
        node = self.launcher[name]
        enabled = node.get("enabled", True)
        if name == "Collect_DebugList_User" and self.debug_enabled:
            enabled = True
        enabled = enabled and name not in self.disabled
        enabled = self.patches.get(name, {}).get("enabled", enabled)
        if name in self.broken:
            return {"enabled": enabled, "recognition": {"type": "And", "param": {}}}
        subs = []
        for sub in node.get("all_of", []):
            if isinstance(sub, dict):
                param = {k: v for k, v in sub.items() if k != "recognition"}
                subs.append({"type": sub.get("recognition"), "param": param})
        action = node.get("action")
        if not isinstance(action, dict):
            action = {
                "type": action,
                "param": {key: node[key] for key in ("custom_action", "custom_action_param") if key in node},
            }
        return {
            "enabled": enabled,
            "recognition": {"type": "And", "param": {"all_of": subs}},
            "action": action,
        }

    def override_pipeline(self, patches):
        self.patches.update(patches)


def card_names(launcher, prefix):
    selector = load_selector(set())
    context = FakeContext(launcher)
    out = {}
    for name in launcher:
        if name.startswith(prefix) and name[len(prefix):].isdigit():
            out[int(name[len(prefix):])] = selector.card_name_of(context.get_node_data(name))
    return out


class CollectConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.selector = load_selector(set())

    @staticmethod
    def _metadata(params):
        return {"action": {"type": "Custom", "param": {
            "custom_action": "PatchPipeline", "custom_action_param": params,
        }}}

    @staticmethod
    def _completion(card="Story_01_血骑士"):
        return {"action": "Custom", "custom_action": "MarkComplete",
                "custom_action_param": {"card_name": card, "cycle_type": "g_weekly"}}

    def test_name_from_success_payload_in_both_patch_forms(self):
        """成功载荷是单卡身份来源；兼容单目标、多目标和逐节点补丁。"""
        payload = self._completion()
        cases = [
            {"target": "Collect_PackEnd_Ck", "patch": payload},
            {"target": ["Collect_PackEnd_Ck", "Extra"], "patch": payload},
            {"patches": {"Collect_PackEnd_Ck": payload, "Other": {"enabled": False}}},
        ]
        for params in cases:
            with self.subTest(params=params):
                data = self._metadata(params)
                before = copy.deepcopy(data)
                self.assertEqual(self.selector.card_name_of(data), "Story_01_血骑士")
                self.assertEqual(data, before)

    def test_missing_or_ambiguous_name_is_not_a_completed_card(self):
        """不猜其他动作、其他出口或畸形载荷，留给选择器保留类别。"""
        payload = self._completion()
        cases = [
            {}, {"target": "Other", "patch": payload}, {"patches": []},
            {"target": "Collect_PackEnd_Ck", "patch": payload, "patches": {}},
            {"target": "Collect_PackEnd_Ck", "patch": self._completion("")},
            {"target": "Collect_PackEnd_Ck", "patch": self._completion(123)},
            {"target": "Collect_PackEnd_Ck", "patch": {"custom_action_param": {"card_name": "wrong"}}},
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertIsNone(self.selector.card_name_of(self._metadata(params)))
        for data in (None, [], {"recognition": {"param": {"all_of": []}}}):
            self.assertIsNone(self.selector.card_name_of(data))

    def test_current_named_cards_all_have_success_metadata(self):
        launcher = load_launcher()
        context = FakeContext(launcher)
        names = [name for name in launcher if PACK_NAME_RE.match(name)]
        self.assertTrue(names)
        for name in names:
            with self.subTest(node=name):
                self.assertEqual(launcher[name]["recognition"], "TemplateMatch")
                self.assertNotIn("all_of", launcher[name])
                self.assertIsInstance(self.selector.card_name_of(context.get_node_data(name)), str)

    def test_debug_list_read_isolated_and_disabled_residue_ignored(self):
        """启用才读取名单；空类、残留 attach 和不同账号的上下文不会相互放行。"""
        launcher = load_launcher()
        configured = {"StoryPack": "1,3~5", "CharacterPack": "", "EventPack": "2，4"}
        disabled = FakeContext(launcher, attach={"Collect_DebugList_User": configured})
        self.assertEqual(self.selector.debug_list_numbers(disabled), {})
        first = FakeContext(launcher, debug_map={}, attach={"Collect_DebugList_User": configured})
        self.assertEqual(self.selector.debug_list_numbers(first), {"StoryPack": {1, 3, 4, 5}, "EventPack": {2, 4}})
        second = FakeContext(launcher, debug_map={"CharacterPack": {3}})
        numbers = self.selector.debug_list_numbers(second)
        self.assertTrue(self.selector.card_in_debug_list("Character_3_美丽无望", numbers))
        self.assertFalse(self.selector.card_in_debug_list("Story_03_雾之叹息", numbers))
        self.assertFalse(self.selector.card_in_debug_list("Unknown_3", numbers))

    def test_debug_list_read_failure_returns_empty(self):
        context = types.SimpleNamespace(get_node_data=lambda _n: None)
        self.assertEqual(self.selector.debug_list_numbers(context), {})


class SelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = load_launcher()
        cls.node = cls.d["Collect_PackType_Select"]
        cls.story = card_names(cls.d, "Collect_Pack_Story_")
        cls.char = card_names(cls.d, "Collect_Pack_Character_")
        cls.event = card_names(cls.d, "Collect_Pack_Event_")

    def _run(self, done, sync_ok=True, **ctx):
        module = load_selector(set(done), sync_ok=sync_ok)
        context = FakeContext(self.d, **ctx)
        argv = types.SimpleNamespace(custom_action_param=json.dumps(self.node["custom_action_param"]))
        result = module.PackTypeSelect().run(context, argv)
        return result, context.patches

    def test_default_story_and_event_pending(self):
        """角色全部已完成 -> 只关角色；剧情、活动有未完成 -> 保留。"""
        _r, patches = self._run(done=[c for n, c in self.char.items() if n <= 7])
        self.assertEqual(patches, {"Collect_FeatureSwitch_CharacterPack": {"enabled": False}})

    def test_legacy_category_marks_do_not_close_pending_types(self):
        """旧存档里的类别标记可以留存；没有单卡完成记录时，三类仍需检查。"""
        _r, patches = self._run(done=[
            "Pack_Story_SimpleDone", "Pack_Character_SimpleDone", "Pack_Event_SimpleDone",
        ])
        self.assertEqual(patches, {})

    def test_blacklisted_undone_card_does_not_keep_type_open(self):
        """★用户的例子:角色 1~6 已完成，7 未完成但被屏蔽 -> 角色整类不进。"""
        done = [c for n, c in self.char.items() if n <= 6]
        _r, patches = self._run(done=done, disabled={"Collect_Pack_Character_7"})
        self.assertIn("Collect_FeatureSwitch_CharacterPack", patches)
        _r, patches = self._run(done=done)
        self.assertNotIn("Collect_FeatureSwitch_CharacterPack", patches)

    def test_type_with_no_enabled_card_is_closed(self):
        """活动 1~5 全被名单关掉 -> 活动整类不进。"""
        _r, patches = self._run(done=[], disabled={f"Collect_Pack_Event_{n}" for n in self.event})
        self.assertEqual(patches, {"Collect_FeatureSwitch_EvenPack": {"enabled": False}})

    def test_debug_card_keeps_type_open_even_if_done(self):
        """调试名单内的卡即使本周已完成也算待采。"""
        all_done = list(self.story.values()) + list(self.char.values()) + list(self.event.values())
        _r, patches = self._run(done=all_done, debug_map={"StoryPack": {3}})
        self.assertNotIn("Collect_FeatureSwitch_StoryPack", patches)
        self.assertIn("Collect_FeatureSwitch_CharacterPack", patches)

    def test_disabled_debug_with_stale_list_does_not_keep_type_open(self):
        all_done = list(self.story.values()) + list(self.char.values()) + list(self.event.values())
        _r, patches = self._run(done=all_done, attach={"Collect_DebugList_User": {"StoryPack": "3"}})
        self.assertIn("Collect_FeatureSwitch_StoryPack", patches)

    def test_unreadable_card_name_keeps_type_open(self):
        """读不到卡名就不关该类(宁可多扫，不可漏采)。"""
        all_done = list(self.story.values()) + list(self.char.values()) + list(self.event.values())
        _r, patches = self._run(done=all_done, broken={"Collect_Pack_Event_2"})
        self.assertNotIn("Collect_FeatureSwitch_EvenPack", patches)
        self.assertIn("Collect_FeatureSwitch_StoryPack", patches)

    def test_sync_failure_stops_without_patching(self):
        result, patches = self._run(done=[], sync_ok=False)
        self.assertFalse(result.success)
        self.assertEqual(patches, {})


# ------------------------------------------------------------------ 范围 / 调试名单
def load_list_patch():
    """用 stub 加载 agent/action/string_processor.py(真实 utils/num_list.py)。"""
    stubs = {}
    for name in ("maa", "maa.agent", "maa.agent.agent_server", "maa.context", "maa.custom_action", "utils"):
        stubs[name] = types.ModuleType(name)
    stubs["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(
        custom_action=lambda _n: (lambda cls: cls))
    stubs["maa.context"].Context = object

    class _FakeAction:
        RunArg = types.SimpleNamespace
        RunResult = types.SimpleNamespace

    stubs["maa.custom_action"].CustomAction = _FakeAction
    stubs["utils"].mfaalog = types.SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("utils.num_list", ROOT / "agent/utils/num_list.py")
        num_list = importlib.util.module_from_spec(spec)
        sys.modules["utils.num_list"] = num_list
        spec.loader.exec_module(num_list)
        spec = importlib.util.spec_from_file_location(
            "string_processor_under_test", ROOT / "agent/action/string_processor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ListPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = load_launcher()
        cls.sp = load_list_patch()
        cls.packs = sorted(k for k in cls.d if PACK_NAME_RE.match(k))

    def _run(self, action, node, attach, **ctx):
        context = FakeContext(self.d, attach={node: attach}, **ctx)
        argv = types.SimpleNamespace(custom_action_param=json.dumps({"node_name": node}))
        getattr(self.sp, action)().run(context, argv)
        return context

    def test_range_is_global(self):
        """自选采集范围全局生效:只填剧情 3 -> 剧情其余、角色、活动全部关闭。"""
        ctx = self._run("WhiteListPatch", "Collect_WhiteList_User", {"StoryPack": "3"})
        on = [k for k in self.packs if ctx.get_node_data(k)["enabled"]]
        self.assertEqual(on, ["Collect_Pack_Story_3"])
        self.assertEqual(ctx.patches["Collect_Pack_Unnamed"], {"enabled": False}, "没有具名节点的新卡也在范围外")

    def test_empty_or_invalid_range_means_off(self):
        """开着却没有有效编号 -> 视为没开，一个节点都不动(否则会一张都不采)。"""
        for attach in ({}, {"StoryPack": "", "CharacterPack": " "}, {"EventPack": "99"}):
            with self.subTest(attach=attach):
                ctx = self._run("WhiteListPatch", "Collect_WhiteList_User", attach)
                self.assertEqual(ctx.patches, {})

    def test_range_never_enables(self):
        """范围只做减法:填了默认不支持的剧情 20、或已被排除的卡，也不会被打开。"""
        ctx = self._run("WhiteListPatch", "Collect_WhiteList_User", {"StoryPack": "5,20"},
                        disabled={"Collect_Pack_Story_5"})
        self.assertNotIn(True, [v.get("enabled") for v in ctx.patches.values()])
        self.assertFalse(ctx.get_node_data("Collect_Pack_Story_20")["enabled"])
        self.assertFalse(ctx.get_node_data("Collect_Pack_Story_5")["enabled"])

    def test_debug_opens_listed_closes_rest(self):
        """调试:名单内启用(含默认不支持的剧情 20)，其余具名卡带与无名卡兜底全部关闭。"""
        ctx = self._run("DebugListPatch", "Collect_DebugList_User", {"StoryPack": "20"})
        on = [k for k in self.packs if ctx.get_node_data(k)["enabled"]]
        self.assertEqual(on, ["Collect_Pack_Story_20"])
        self.assertEqual(ctx.patches["Collect_Pack_Unnamed"], {"enabled": False})

    def test_range_then_selector(self):
        """串联:范围只填剧情 3(未完成) -> 目标选择器保留剧情，角色与活动因无启用卡带整类关闭。"""
        ctx = self._run("WhiteListPatch", "Collect_WhiteList_User", {"StoryPack": "3"})
        node = self.d["Collect_PackType_Select"]
        argv = types.SimpleNamespace(custom_action_param=json.dumps(node["custom_action_param"]))
        load_selector(set()).PackTypeSelect().run(ctx, argv)
        self.assertNotIn("Collect_FeatureSwitch_StoryPack", ctx.patches)
        self.assertEqual(ctx.patches["Collect_FeatureSwitch_CharacterPack"], {"enabled": False})
        self.assertEqual(ctx.patches["Collect_FeatureSwitch_EvenPack"], {"enabled": False})


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
            custom_action_param=json.dumps({"tags": [t for _n, t, _s, _m in BRANCHES]}))
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
        for node, tag, _setup, _mark in BRANCHES:
            with self.subTest(node=node):
                self.assertTrue(self._admit(node, tag))

    def test_after_real_touchdown_story_not_reentered(self):
        """剧情正常扫完一轮(列表开着触底)⇒ 置位 ⇒ 不再进入，落到下一类。"""
        node, tag, _setup, mark = BRANCHES[0]
        self.assertTrue(self._admit(node, tag))
        self.assertTrue(self._touchdown(mark, tag, quickcart_open=True))
        self.assertFalse(self._admit(node, tag))
        # 角色/活动不受剧情标记影响
        self.assertTrue(self._admit(BRANCHES[1][0], BRANCHES[1][1]))

    def test_bogus_touchdown_does_not_lock_the_class(self):
        """★列表没开的误触底(2026-10-07 已知 bug)不打标记 ⇒ 本类仍可进入。"""
        node, tag, _setup, mark = BRANCHES[0]
        self.assertTrue(self._admit(node, tag))
        self.assertFalse(self._touchdown(mark, tag, quickcart_open=False))
        self.assertTrue(self._admit(node, tag), "误触底不应封死本类")

    def test_new_round_reset_clears_marks(self):
        """新一轮入口 Reset 后标记归零 ⇒ 剧情重新可进(停止重跑场景)。"""
        node, tag, _setup, mark = BRANCHES[0]
        self.assertTrue(self._admit(node, tag))
        self.assertTrue(self._touchdown(mark, tag, quickcart_open=True))
        self.assertFalse(self._admit(node, tag))
        self._reset_all()
        self.assertTrue(self._admit(node, tag))

    def test_max_hit_caps_at_three(self):
        """就算一直没触底，同一类最多进 3 次 ⇒ 不会无限重扫(防死循环)。"""
        node, tag, _setup, _mark = BRANCHES[0]
        self.assertEqual([self._admit(node, tag) for _ in range(4)],
                         [True, True, True, False])


if __name__ == "__main__":
    unittest.main()
