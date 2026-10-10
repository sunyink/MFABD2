"""Offline checks for the pack badge recognition (PackBadgeNotDone).

三件事:
  1. 徽章三态判定 —— 用 2026-10-01 实机截图 (install/debug/on_error) 当夹具,
     回归「勾=采完」判据: 勾区亮像素 有勾 11~13 / 无勾 0~2,间隔必须保持干净;
  2. 无徽章兜底 —— 币都找不到时按用户口径视为未完成 (need_collect=True);
  3. 静态检查 Collect_Launcher.json —— 33 个 Collect_Pack_* 必须用图标判据
     (PackBadgeNotDone + [Anchor]PackFrame_Loc + max_hit),SimpleSkip 存档闸必须
     已拆除,Collect_AllLibGone 必须停用。

跑法(仓库根):  python -m unittest tools.test_pack_badge
截图缺失时三态用例自动 skip(其余用例照跑)。
"""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

# 2026-10-01 实机截图:角色游戏卡页,锚点框(143,593),6 张卡带可见。
# 币对 x 起点(星币): 28/174/316/460/604/748 -> 锚点框 x0 = 星币x - 28。
SCREENSHOT = ROOT / "install/debug/on_error/2026.10.01-10.39.12.222_Collect_PackLocation_PassFieldsAndHub.png"
ANCHOR_Y = 593
SLOT_ANCHOR_X = [0, 146, 288, 432, 576, 720]
# 实测状态: cart2 双勾 / cart3 全彩无勾 / cart4 星勾+骷髅红 / cart5-7 双勾
SLOT_EXPECT = [False, True, True, False, False, False]


def load_pack_badge():
    import PIL.Image  # noqa: F401  先灌进 sys.modules:stub 环境里 exec 若再走一遍
    # PIL.Image 初始化会得到一个插件注册不全的副本，Image.open 直接不认 PNG
    stubs = {}
    for name in ("maa", "maa.agent", "maa.agent.agent_server", "maa.custom_recognition", "maa.context"):
        stubs[name] = types.ModuleType(name)
    stubs["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(
        custom_recognition=lambda _name: (lambda cls: cls)
    )
    class _FakeCustomRecognition:
        AnalyzeArg = types.SimpleNamespace
        AnalyzeResult = types.SimpleNamespace

    stubs["maa.custom_recognition"].CustomRecognition = _FakeCustomRecognition
    stubs["maa.context"].Context = object
    utils = types.ModuleType("utils")
    utils.__path__ = [str(ROOT / "agent/utils")]  # 声明成包，否则 import 子模块会报 not a package
    utils.mfaalog = types.SimpleNamespace(
        info=lambda *a, **k: None, debug=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    stubs["utils"] = utils
    # pack_badge 里有 from utils.num_list import parse_number_list；num_list 只依赖 re，
    # 按真实文件加载后挂到 utils 上（stub 的 utils 没有这个子模块，不预置会 ModuleNotFoundError）
    num_list_spec = importlib.util.spec_from_file_location("utils.num_list", ROOT / "agent/utils/num_list.py")
    num_list_mod = importlib.util.module_from_spec(num_list_spec)
    num_list_spec.loader.exec_module(num_list_mod)
    utils.num_list = num_list_mod
    stubs["utils.num_list"] = num_list_mod
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("pack_badge_under_test", ROOT / "agent/recognition/pack_badge.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["pack_badge_under_test"] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("pack_badge_under_test", None)
    return module


def load_pipeline(rel):
    with open(ROOT / rel, "r", encoding="utf-8") as f:
        return json.load(f)


def load_launcher():
    return load_pipeline("assets/resource/base/pipeline/Collect_Launcher.json")


# ---------------------------------------------------------------- 徽章判定
class BadgeClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pb = load_pack_badge()
        if not SCREENSHOT.exists():
            raise unittest.SkipTest(f"夹具截图不存在: {SCREENSHOT}")
        from PIL import Image
        cls.hsv = np.array(Image.open(SCREENSHOT).convert("RGB").convert("HSV"))

    def badges(self, anchor_x, anchor_y=ANCHOR_Y):
        sb = self.pb.STAR_COIN
        kb = self.pb.SKULL_COIN
        return self.pb.analyze_badges(
            self.hsv,
            (anchor_x + sb[0], anchor_y + sb[1], sb[2], sb[3]),
            (anchor_x + kb[0], anchor_y + kb[1], kb[2], kb[3]),
        )

    def test_known_slots_match_field_truth(self):
        for x, expect_need in zip(SLOT_ANCHOR_X, SLOT_EXPECT):
            rep = self.badges(x)
            self.assertEqual(
                rep["need_collect"], expect_need,
                f"锚点 x={x}: need_collect 应为 {expect_need},实际 {rep}",
            )

    def test_checked_margin_is_clean(self):
        """有勾的勾区像素必须 ≥2 倍阈值,无勾必须 < 阈值 —— 防夹具漂移吃掉余量。"""
        for x, expect_need in zip(SLOT_ANCHOR_X, SLOT_EXPECT):
            rep = self.badges(x)
            for coin in ("star", "skull"):
                px = rep[coin]["check_px"]
                if expect_need and rep[coin]["checked"]:
                    self.assertGreaterEqual(px, 2 * self.pb.CHK_MIN_PX, f"x={x} {coin}")
                if not rep[coin]["checked"]:
                    self.assertLess(px, self.pb.CHK_MIN_PX, f"x={x} {coin}")

    def test_missing_badges_means_unsupported_pack(self):
        """不支持采集的卡带下方没有徽章,不能当成"没采完"反复重进。"""
        rep = self.badges(900)  # 该帧右下角几张卡带没有徽章
        self.assertTrue(rep["no_badge"])
        self.assertFalse(rep["need_collect"])
        self.assertFalse(rep["star"]["coin_seen"])
        self.assertFalse(rep["skull"]["coin_seen"])

    def test_coin_presence_margin_is_clean(self):
        """有徽章的卡带币面像素必须远高于存在阈值,否则"无徽章"兜底会误伤。"""
        for x in SLOT_ANCHOR_X:
            rep = self.badges(x)
            for coin in ("star", "skull"):
                self.assertTrue(rep[coin]["coin_seen"], f"x={x} {coin} 应检出币")
                self.assertGreaterEqual(
                    rep[coin]["face_px"], 2 * self.pb.FACE_MIN_PX, f"x={x} {coin}"
                )

    def test_pattern_scores_true_coins_pass(self):
        """图案比对:真星/骷髅币与各自模板的 NCC 必须过阈值(含跨状态泛化)。"""
        sb, kb = self.pb.STAR_COIN, self.pb.SKULL_COIN
        for x in SLOT_ANCHOR_X:
            ps = self.pb.pattern_scores(
                self.hsv,
                (x + sb[0], ANCHOR_Y + sb[1], sb[2], sb[3]),
                (x + kb[0], ANCHOR_Y + kb[1], kb[2], kb[3]),
            )
            self.assertGreaterEqual(ps["star"], self.pb.BADGE_TPL_TH, f"x={x}")
            self.assertGreaterEqual(ps["skull"], self.pb.BADGE_TPL_TH, f"x={x}")

    def test_pattern_scores_reject_foreign_coins(self):
        """租借卡之类:双圆币但图案不是星/骷髅 -> 图案比对必须拒绝。

        用夹具图合成:抹掉 cart3 双币,画两枚白圆内加深灰方块图案。
        """
        hsv = self.hsv.copy()
        ax = SLOT_ANCHOR_X[2]
        hsv[674:706, ax + 20:ax + 100] = (110, 40, 60)   # 抹成深底
        for cx in (ax + 41, ax + 73):                     # 两枚白圆内的方块图案
            hsv[680:698, cx:cx + 20] = (0, 0, 150)
        sb, kb = self.pb.STAR_COIN, self.pb.SKULL_COIN
        ps = self.pb.pattern_scores(
            hsv,
            (ax + sb[0], ANCHOR_Y + sb[1], sb[2], sb[3]),
            (ax + kb[0], ANCHOR_Y + kb[1], kb[2], kb[3]),
        )
        self.assertLess(ps["star"], self.pb.BADGE_TPL_TH, str(ps))
        self.assertLess(ps["skull"], self.pb.BADGE_TPL_TH, str(ps))


# ---------------------------------------------------------------- 无名卡带兜底
class UnnamedPackTests(unittest.TestCase):
    """黑名单优先于"未双勾"：屏蔽的卡带即使没勾也不采。"""

    @classmethod
    def setUpClass(cls):
        cls.pb = load_pack_badge()
        if not SCREENSHOT.exists():
            raise unittest.SkipTest(f"夹具截图不存在: {SCREENSHOT}")
        from PIL import Image
        rgb = np.array(Image.open(SCREENSHOT).convert("RGB"))
        cls.bgr = rgb[..., ::-1]

    def context(self, disabled=(), node_list=None, hit_nodes=()):
        names = list(node_list if node_list is not None else
                     ["Collect_Pack_Story_1", "Collect_Pack_Story_3", "Collect_Pack_Character_8"])
        data = {n: {"enabled": False} if n in disabled else {"enabled": True} for n in names}
        overrides = []

        class _Res:
            def __init__(self, hit):
                self.hit = hit

        class _Ctx:
            def __init__(self):
                self.tasker = types.SimpleNamespace(
                    resource=types.SimpleNamespace(node_list=names))

            def get_node_data(self, name):
                return data.get(name)

            def run_recognition(self, name, image, override=None):
                overrides.append((name, override))
                return _Res(name in hit_nodes)

        ctx = _Ctx()
        ctx.overrides = overrides
        return ctx

    def test_disabled_pack_nodes_filters(self):
        ctx = self.context(disabled=("Collect_Pack_Character_8",))
        self.assertEqual(self.pb.disabled_pack_nodes(ctx), ["Collect_Pack_Character_8"])

    def test_blacklisted_slot_is_detected(self):
        ctx = self.context(disabled=("Collect_Pack_Story_3",), hit_nodes=("Collect_Pack_Story_3",))
        blacked, who = self.pb.is_blacklisted_slot(ctx, self.bgr)
        self.assertTrue(blacked)
        self.assertEqual(who, ["Collect_Pack_Story_3"])
        # 禁用节点必须靠 override 临时启用，否则框架直接返回 None
        self.assertEqual(ctx.overrides, [("Collect_Pack_Story_3",
                                          {"Collect_Pack_Story_3": {"enabled": True}})])

    def test_no_blacklist_means_not_blacklisted(self):
        ctx = self.context()
        self.assertFalse(self.pb.is_blacklisted_slot(ctx, self.bgr)[0])
        self.assertEqual(ctx.overrides, [])  # 没有禁用节点就一次都不该问

    def test_blacklisted_pack_is_not_collected(self):
        """黑名单卡带即便没双勾也必须跳过。"""
        node = self.pb.PackUnnamedNotDone()
        ctx = self.context(disabled=("Collect_Pack_Story_3",), hit_nodes=("Collect_Pack_Story_3",))
        argv = types.SimpleNamespace(
            roi=types.SimpleNamespace(x=146, y=ANCHOR_Y, w=114, h=78),
            image=self.bgr,
            custom_recognition_param="{}",
        )
        # 该锚点对应 cart3（未采集，星骷髅都没勾）
        self.assertIsNone(node.analyze(ctx, argv))

    def test_unchecked_pack_is_collected_when_not_blacklisted(self):
        """不在黑名单里的未双勾卡带要采。"""
        node = self.pb.PackUnnamedNotDone()
        ctx = self.context()
        argv = types.SimpleNamespace(
            roi=types.SimpleNamespace(x=146, y=ANCHOR_Y, w=114, h=78),
            image=self.bgr,
            custom_recognition_param="{}",
        )
        res = node.analyze(ctx, argv)
        self.assertIsNotNone(res)
        self.assertEqual(res.box, [146, ANCHOR_Y, 114, 78])

    def test_checked_pack_is_skipped(self):
        """已双勾的卡带不该由兜底节点处理（具名节点也不该，见 BadgeClassificationTests）。"""
        node = self.pb.PackUnnamedNotDone()
        ctx = self.context()
        argv = types.SimpleNamespace(
            roi=types.SimpleNamespace(x=0, y=ANCHOR_Y, w=114, h=78),  # cart2 双勾
            image=self.bgr,
            custom_recognition_param="{}",
        )
        self.assertIsNone(node.analyze(ctx, argv))


# ---------------------------------------------------------------- pipeline 静态检查
class CollectLauncherLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_launcher()

    def pack_nodes(self):
        return {
            name: node for name, node in self.data.items()
            if name.startswith("Collect_Pack_")
            and any(name.startswith(f"Collect_Pack_{t}_") for t in ("Story", "Character", "Event"))
        }

    def test_pack_count(self):
        self.assertEqual(len(self.pack_nodes()), 33)

    def test_pack_uses_badge_recognition(self):
        for name, node in self.pack_nodes().items():
            subs = node.get("all_of")
            self.assertIsInstance(subs, list, f"{name} 应保留 all_of 组合识别")
            customs = [s for s in subs if s.get("custom_recognition")]
            self.assertEqual(len(customs), 1, f"{name} 应恰有一个自定义识别子节点")
            self.assertEqual(customs[0]["custom_recognition"], "PackBadgeNotDone", name)
            self.assertEqual(customs[0].get("roi"), "[Anchor]PackFrame_Loc", name)
            blob = json.dumps(node, ensure_ascii=False)
            self.assertNotIn("CheckCoolDown", blob, f"{name} 不得再用存档闸判采集")

    def test_pack_has_max_hit_guard(self):
        """采不完的卡带会持续命中,重进必须有运行级上限防死循环。"""
        for name, node in self.pack_nodes().items():
            self.assertGreaterEqual(node.get("max_hit", 0), 1, name)

    def test_simple_skip_gates_removed(self):
        for name in ("Collect_StoryPack_SetSimpleSkip",
                     "Collect_CharacterPack_SetSimpleSkip",
                     "Collect_EventPack_SetSimpleSkip"):
            node = self.data[name]
            self.assertNotIn("custom_recognition", node, f"{name} 的存档闸没拆")
            self.assertNotIn("recognition", node, f"{name} 应为 DirectHit")

    def test_all_lib_gone_disabled(self):
        """全类 SimpleDone 总闸会整周跳过未采完的卡带,必须停用。"""
        self.assertFalse(self.data["Collect_AllLibGone"].get("enabled", True))

    def test_unnamed_fallback_node_wired(self):
        """没有具名节点的卡带也要能被采，但必须排在具名候选项之后并先过黑名单。"""
        node = self.data["Collect_Pack_Unnamed"]
        self.assertEqual(node["custom_recognition"], "PackUnnamedNotDone")
        self.assertEqual(node["roi"], "[Anchor]PackFrame_Loc")
        self.assertGreaterEqual(node["max_hit"], 2)
        self.assertEqual(node["anchor"], "Pack_Click")
        self.assertEqual(node["next"], ["Collect_Pack_Click"])
        nxt = self.data["Collect_PackLocation_PassFieldsAndHub"]["next"]
        self.assertEqual(nxt[-1], "Collect_Pack_Unnamed",
                         "兜底必须排在最后：先让具名节点认领，剩下的才按槽位采")
        self.assertNotIn("Collect_Pack_Unnamed", nxt[:-1])

    def test_smart_swip_retry_bumped(self):
        param = self.data["Collect_LocatePackFrame_Smart_Swip"]["custom_action_param"]
        self.assertGreaterEqual(param.get("retry_times", 1), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
