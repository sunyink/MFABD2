"""rdd_hsv_rescue 的纯算法单测（不依赖 MaaFramework，不碰识别器主流程）。

跑法（三选一，都从仓库根执行）：
    python agent/recognition/test_rdd_hsv_rescue.py
    python -m unittest discover -s agent/recognition -p 'test_*.py'
    cd agent && python -m unittest recognition.test_rdd_hsv_rescue

⚠️ `python -m unittest agent.recognition.test_rdd_hsv_rescue` 用不了，且不该被"修好"。
本项目的包根是 `agent/` 而不是仓库根 —— agent/main.py 把 agent/ 插进 sys.path 后
`import recognition`，`agent` 目录本身没有 __init__.py、不是包。`agent.recognition.*`
只是 Python 3 隐式命名空间包的副产物，走这条路会先执行 agent/recognition/__init__.py
里的 `from .counter import *`，其中的 `from utils import mfaalog` 在仓库根 cwd 下
解析不到，报 ModuleNotFoundError: No module named 'utils'。要让它可用就得在生产代码
里塞一套只为测试服务的 sys.path 兜底，凭空多出第二套与运行时不一致的导入契约，
不划算。上面三条跑法都落在 `recognition` 这个正确的包根上。
"""

import unittest

import numpy as np

try:
    from .rdd_hsv_rescue import (
        bloated_hits,
        boxes_agree,
        channel_cutpoints,
        cut_happened,
        direction_cut,
        direction_deltas,
        hits_agree,
        hue_axis,
        hue_cutpoint,
        hue_keep_for,
        hue_neighbor_states,
        is_strict_mask,
        lineage_parent,
        neighbor_states,
        normalize_rescue_config,
        otsu_threshold,
        pixel_set_key,
        resolve_entry_plan,
        select_stable_winner,
        sort_parents,
        strict_profile,
    )
except ImportError:
    from rdd_hsv_rescue import (
        bloated_hits,
        boxes_agree,
        channel_cutpoints,
        cut_happened,
        direction_cut,
        direction_deltas,
        hits_agree,
        hue_axis,
        hue_cutpoint,
        hue_keep_for,
        hue_neighbor_states,
        is_strict_mask,
        lineage_parent,
        neighbor_states,
        normalize_rescue_config,
        otsu_threshold,
        pixel_set_key,
        resolve_entry_plan,
        select_stable_winner,
        sort_parents,
        strict_profile,
    )


RANGES = [
    {"lower": [0, 140, 120], "upper": [12, 255, 255]},
    {"lower": [165, 140, 120], "upper": [180, 255, 255]},
]


class RescueConfigTest(unittest.TestCase):
    def test_default_is_off(self):
        config, error = normalize_rescue_config(None)
        self.assertIsNone(error)
        self.assertEqual(config["mode"], "off")

    def test_invalid_config_fails_closed(self):
        config, error = normalize_rescue_config(
            {"mode": "active", "min_stable_states": 3, "max_full_runs": 2})
        self.assertIsNotNone(error)
        self.assertEqual(config["mode"], "off")

        config, error = normalize_rescue_config(
            {"mode": "active", "max_parents": 100})
        self.assertIsNotNone(error)
        self.assertEqual(config["mode"], "off")

    def test_entry_defaults_are_the_target_state(self):
        """不写 entry_* 时缺省值就是目标状态：aspect 行为不变、rej 只观测。"""
        config, error = normalize_rescue_config({"mode": "active"})
        self.assertIsNone(error)
        self.assertEqual(config["entry_aspect"], "inherit")
        self.assertEqual(config["entry_confidence"], "inherit")
        self.assertEqual(config["entry_confidence_rej"], "shadow")
        # aspect 帧：与加入口之前逐位一致(唯一入口、active)
        self.assertEqual(
            resolve_entry_plan(config, "aspect"),
            [("entry_aspect", "aspect", "active")])
        # confidence 帧：主诉入口可改判，兜底入口只观测
        self.assertEqual(
            resolve_entry_plan(config, "confidence"),
            [("entry_confidence", "confidence", "active"),
             ("entry_confidence_rej", "aspect", "shadow")])

    def test_entry_switch_overrides_global_mode(self):
        """分开关的意义就在于：A 保持 active 的同时把新入口挂 shadow / 关掉。"""
        config, error = normalize_rescue_config({
            "mode": "active", "entry_confidence": "shadow",
            "entry_confidence_rej": "off"})
        self.assertIsNone(error)
        self.assertEqual(
            resolve_entry_plan(config, "confidence"),
            [("entry_confidence", "confidence", "shadow")])
        # aspect 帧不受新开关影响
        self.assertEqual(
            resolve_entry_plan(config, "aspect"),
            [("entry_aspect", "aspect", "active")])

    def test_entry_plan_order_is_confidence_then_rej(self):
        """入口顺序固定：主诉(打分不足的块)先试，同帧被拒块兜底。"""
        config, _ = normalize_rescue_config({"mode": "active"})
        self.assertEqual(
            [k for k, _, _ in resolve_entry_plan(config, "confidence")],
            ["entry_confidence", "entry_confidence_rej"])

    def test_entry_plan_empty_when_off(self):
        config, _ = normalize_rescue_config({"mode": "off"})
        self.assertEqual(resolve_entry_plan(config, "aspect"), [])
        self.assertEqual(resolve_entry_plan(config, "confidence"), [])
        # mode 是硬总开关：入口自己写 active 也压不过它
        config, _ = normalize_rescue_config(
            {"mode": "off", "entry_confidence": "active"})
        self.assertEqual(resolve_entry_plan(config, "confidence"), [])
        # 没有入口表的阶段(red_mask/area/interior)一律不搜索
        config, _ = normalize_rescue_config({"mode": "active"})
        self.assertEqual(resolve_entry_plan(config, "interior"), [])

    def test_invalid_entry_value_fails_closed(self):
        config, error = normalize_rescue_config(
            {"mode": "active", "entry_confidence": "yes"})
        self.assertIsNotNone(error)
        self.assertEqual(config["mode"], "off")

    def test_obsolete_key_is_ignored_not_fatal(self):
        """旧 pipeline 残留的 max_states 不应把救援直接关掉。"""
        config, error = normalize_rescue_config(
            {"mode": "shadow", "max_states": 64})
        self.assertIsNone(error)
        self.assertEqual(config["mode"], "shadow")
        self.assertNotIn("max_states", config)

    def test_strict_profile_only_raises_sv_lower(self):
        profile = strict_profile(RANGES, 12, 18)
        self.assertEqual(profile[0]["lower"], [0, 152, 138])
        self.assertEqual(profile[1]["lower"], [165, 152, 138])
        self.assertEqual(profile[0]["upper"], RANGES[0]["upper"])

    def test_strict_profile_fails_closed_when_lower_crosses_upper(self):
        """收紧把 lower 顶过 upper 时必须 fail closed。

        构造要点：新 lower 有 `min(255, ...)` 钳位，所以只有 upper 的 S 或 V
        **小于 255** 时才够得到这个分支。拿 upper 全 255 的 RANGES 配再大的
        delta 都撞不出来 —— 这里容易凭直觉写出永远不成立的用例。
        """
        narrow_s = [{"lower": [0, 140, 120], "upper": [12, 200, 255]}]
        self.assertIsNone(strict_profile(narrow_s, 80, 0))       # 140+80 > 200
        self.assertIsNotNone(strict_profile(narrow_s, 60, 0))    # 140+60 = 200，贴边不算越界
        narrow_v = [{"lower": [0, 140, 120], "upper": [12, 255, 180]}]
        self.assertIsNone(strict_profile(narrow_v, 0, 61))       # 120+61 > 180
        # upper 全 255 时钳位兜底，无论多大都收敛成合法 profile，不该误判成 fail closed
        self.assertEqual(strict_profile(RANGES, 10000, 10000)[0]["lower"], [0, 255, 255])

    def test_strict_profile_allows_single_axis_tightening(self):
        """单边 delta=0 是合法档，**不得**拒 —— 拒了等于废掉三个切法里的两个。

        `direction_deltas` 给出的是 亮度=(0, dv)、饱和=(ds, 0)、双切=(ds, dv)，
        单边为 0 正是前两者的常态；实测语料里绝大多数救援命中走的就是 亮度 档。
        只有两边都不收紧才没救援可做。
        """
        self.assertEqual(strict_profile(RANGES, 0, 18)[0]["lower"], [0, 140, 138])
        self.assertEqual(strict_profile(RANGES, 12, 0)[0]["lower"], [0, 152, 120])
        self.assertIsNone(strict_profile(RANGES, 0, 0))
        self.assertIsNone(strict_profile(RANGES, -1, 18))
        self.assertIsNone(strict_profile(RANGES, 12, -1))

    def test_strict_profile_rejects_malformed_ranges(self):
        self.assertIsNone(strict_profile(
            [{"lower": [0, 140], "upper": [12, 255, 255]}], 12, 18))
        self.assertIsNone(strict_profile([{"upper": [12, 255, 255]}], 12, 18))
        self.assertIsNone(strict_profile([{"lower": [0, 140, 120]}], 12, 18))
        self.assertIsNone(strict_profile([], 12, 18))


class CutpointAndCandidateTest(unittest.TestCase):
    def test_candidate_mask_must_be_true_subset(self):
        baseline = np.array([[1, 1], [0, 1]], dtype=bool)
        same = baseline.copy()
        strict = np.array([[1, 0], [0, 1]], dtype=bool)
        wider = np.array([[1, 1], [1, 1]], dtype=bool)
        self.assertFalse(is_strict_mask(same, baseline))
        self.assertTrue(is_strict_mask(strict, baseline))
        self.assertFalse(is_strict_mask(wider, baseline))

    def test_otsu_splits_two_peaks_and_refuses_degenerate(self):
        two_peaks = np.array([150] * 40 + [220] * 40, dtype=np.uint8)
        cut = otsu_threshold(two_peaks)
        self.assertIsNotNone(cut)
        self.assertTrue(150 < cut <= 220, cut)
        self.assertIsNone(otsu_threshold(np.full(40, 180, dtype=np.uint8)))
        self.assertIsNone(otsu_threshold(np.array([1, 2, 3], dtype=np.uint8)))

    def test_cutpoints_come_from_parent_pixels_and_respect_cap(self):
        """切点由父块自身分布算出；护栏只封顶，不充当工作点。"""
        pixels = np.zeros((80, 3), dtype=np.uint8)
        pixels[:, 1] = [160] * 40 + [230] * 40     # 饱和度双峰
        pixels[:, 2] = [130] * 40 + [210] * 40     # 亮度双峰
        config, _ = normalize_rescue_config({"mode": "shadow"})
        cuts = channel_cutpoints(pixels, RANGES, config)
        self.assertGreater(cuts["亮度"]["增量"], 0)
        self.assertGreater(cuts["饱和"]["增量"], 0)
        self.assertEqual(cuts["亮度"]["基线"], 120)
        self.assertEqual(cuts["饱和"]["基线"], 140)

        tight, _ = normalize_rescue_config(
            {"mode": "shadow", "max_delta_v": 5, "max_delta_s": 5})
        capped = channel_cutpoints(pixels, RANGES, tight)
        self.assertEqual(capped["亮度"]["增量"], 5)
        self.assertEqual(capped["饱和"]["增量"], 5)

    def test_direction_deltas_cover_three_cuts(self):
        cuts = {"饱和": {"增量": 40}, "亮度": {"增量": 50}}
        self.assertEqual(direction_deltas(cuts, "亮度"), (0, 50))
        self.assertEqual(direction_deltas(cuts, "饱和"), (40, 0))
        self.assertEqual(direction_deltas(cuts, "双切"), (40, 50))
        # 某通道无增量时，该方向不成立（避免生成 (0,0) 的空收紧）
        flat = {"饱和": {"增量": 0}, "亮度": {"增量": 0}}
        self.assertIsNone(direction_deltas(flat, "双切"))

    def test_neighbor_states_are_adjacent_and_bounded(self):
        config, _ = normalize_rescue_config({"mode": "shadow"})
        states = neighbor_states("亮度", 0, 50, config)
        self.assertEqual(len(states), 3)
        self.assertEqual({s["si"] for s in states}, {0})
        self.assertEqual([s["vi"] for s in states], [0, 1, 2])
        self.assertEqual([s["delta_v"] for s in states], [45, 50, 55])
        self.assertTrue(all(s["delta_s"] == 0 for s in states))

        # 越过护栏的档位被剔除，不会溢出上限
        tight, _ = normalize_rescue_config(
            {"mode": "shadow", "max_delta_v": 52})
        bounded = neighbor_states("亮度", 0, 50, tight)
        self.assertTrue(all(s["delta_v"] <= 52 for s in bounded))
        self.assertLess(len(bounded), 3)

        # 不同切法落在互不相邻的 si 上，避免跨方向连通成同一分量
        self.assertNotEqual(neighbor_states("亮度", 0, 50, config)[0]["si"],
                            neighbor_states("双切", 40, 50, config)[0]["si"])

    def test_sort_parents_puts_thick_blocks_first(self):
        """短边越大越像红点；实测 boss-adb 的正主(53x16)应排第一。"""
        parents = [
            {"label": 1, "w": 34, "h": 5, "area": 103},
            {"label": 2, "w": 53, "h": 16, "area": 239},
            {"label": 3, "w": 6, "h": 15, "area": 52},
            {"label": 4, "w": 50, "h": 15, "area": 258},
        ]
        order = [p["label"] for p in sort_parents(parents)]
        self.assertEqual(order[0], 2)
        self.assertEqual(order[-1], 1)

    def test_cut_happened_rejects_identical_box(self):
        """外接框与父块逐位相同 = 一次连片都没切断，不算救援成立。"""
        parent = {"x": 0, "y": 0, "w": 30, "h": 32}
        self.assertFalse(cut_happened((0, 0, 30, 32), parent))
        # 真救援：连片被切开，框实质缩小（GachaADV_Location1 实测形态）
        self.assertTrue(cut_happened((25, 7, 18, 17),
                                     {"x": 8, "y": 7, "w": 35, "h": 29}))
        # 尺寸相同但位置移动，也算切开了（父块像素被删后整体挪位）
        self.assertTrue(cut_happened((1, 0, 30, 32), parent))

    def test_boxes_agree_rejects_scattered_hits(self):
        same = [(10, 10, 12, 12), (10, 11, 12, 12)]
        apart = [(10, 10, 12, 12), (60, 60, 12, 12)]
        self.assertTrue(boxes_agree(same))
        self.assertFalse(boxes_agree(apart))
        self.assertTrue(boxes_agree([(0, 0, 5, 5)]))

    def test_boxes_agree_tolerates_uneven_cut(self):
        """不同切法切净程度不同 → 框一大一小、中心偏移，但仍是同一个目标。

        GachaADV_Location1 真机实测三框：亮度那档左边多包 6px 木纹没切干净，
        中心比另两档偏 3.0px。旧判据(中心位移 ≤2.5)据此判方向歧义、救援 fail closed，
        而 IoU 0.718/0.750 说明它们大量重合，本就是同一处。
        """
        bright = (19, 7, 24, 17)      # 没切干净
        sat = (25, 6, 18, 18)
        both = (25, 7, 18, 17)
        self.assertTrue(boxes_agree([bright, sat, both]))
        # 真歧义仍拒：两个不重叠的红块 IoU = 0
        self.assertFalse(boxes_agree([bright, (60, 60, 18, 18)]))

    def test_boxes_agree_checks_every_pair(self):
        """IoU ≥ 0.5 不传递：只跟第一个比会漏判。

        B、C 各占 A 的一半且都是 A 的子集，IoU(A,B)=IoU(A,C)=0.5 双双过闸，
        而 B∩C=∅ —— 它们指向的是两个不相干的位置。
        """
        whole = (0, 0, 100, 100)
        left = (0, 0, 50, 100)
        right = (50, 0, 50, 100)
        self.assertFalse(boxes_agree([whole, left, right]))


class LineageAndStabilityTest(unittest.TestCase):
    def test_lineage_requires_one_eligible_parent(self):
        labels = np.array([[1, 1, 0], [0, 2, 2]], dtype=np.int32)
        child = np.array([[1, 0, 0], [0, 0, 0]], dtype=bool)
        mixed = np.array([[1, 0, 0], [0, 1, 0]], dtype=bool)
        self.assertEqual(lineage_parent(child, labels, [1]), 1)
        self.assertIsNone(lineage_parent(child, labels, [2]))
        self.assertIsNone(lineage_parent(mixed, labels, [1, 2]))

    def test_two_adjacent_states_form_unique_stable_winner(self):
        records = [
            {"parent_id": 1, "box_local": (10, 10, 12, 12),
             "state": {"si": 1, "vi": 0, "delta_s": 5, "delta_v": 0},
             "scan_index": 1},
            {"parent_id": 1, "box_local": (10, 10, 11, 12),
             "state": {"si": 2, "vi": 0, "delta_s": 8, "delta_v": 0},
             "scan_index": 1},
        ]
        winner, reason, support = select_stable_winner(records, 2)
        self.assertEqual(reason, "stable_hit")
        self.assertIs(winner, records[0])
        self.assertEqual(len(support), 1)

    def test_single_state_and_multiple_tracks_fail_closed(self):
        one = [{"parent_id": 1, "box_local": (0, 0, 10, 10),
                "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}}]
        winner, reason, _ = select_stable_winner(one, 2)
        self.assertIsNone(winner)
        self.assertEqual(reason, "unstable_hit")

        ambiguous = [
            {"parent_id": 1, "box_local": (0, 0, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
            {"parent_id": 1, "box_local": (0, 0, 10, 10),
             "state": {"si": 2, "vi": 0, "delta_s": 2, "delta_v": 0}},
            {"parent_id": 2, "box_local": (20, 20, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
            {"parent_id": 2, "box_local": (20, 20, 10, 10),
             "state": {"si": 2, "vi": 0, "delta_s": 2, "delta_v": 0}},
        ]
        winner, reason, _ = select_stable_winner(ambiguous, 2)
        self.assertIsNone(winner)
        self.assertEqual(reason, "ambiguous_stable_hits")

    def test_split_in_same_parent_state_is_ambiguous(self):
        records = [
            {"parent_id": 1, "box_local": (0, 0, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
            {"parent_id": 1, "box_local": (20, 0, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
        ]
        winner, reason, _ = select_stable_winner(records, 2)
        self.assertIsNone(winner)
        self.assertEqual(reason, "ambiguous_split")


class HueCutTest(unittest.TestCase):
    """色相后手：展开轴上一刀、两侧各试一次（契约 §4.1，起因 GachaADV_Location1 2026-10-28）。"""

    @staticmethod
    def _pil_h(h_cv):
        return int(round(h_cv * 255.0 / 180.0))

    def test_hue_axis_unwraps_the_two_red_bands(self):
        """[0,12]∪[165,180] 展开成以纯红为 0 的一段 [-15,12]。"""
        self.assertEqual(hue_axis(RANGES), (-15, 12))
        self.assertEqual(hue_axis([{"lower": [100, 50, 50], "upper": [130, 255, 255]}]),
                         (-80, -50))
        self.assertIsNone(hue_axis([{"lower": [0, 1], "upper": [12, 255, 255]}]))

    def test_strict_profile_hue_keep_trims_bands_and_stays_subset(self):
        # 保留上侧 [-4,12]：橙红段不动，品红段下沿 165 → 176
        up = strict_profile(RANGES, 0, 0, (-4, 12))
        self.assertEqual(up, [{"lower": [0, 140, 120], "upper": [12, 255, 255]},
                              {"lower": [176, 140, 120], "upper": [180, 255, 255]}])
        # 保留下侧 [-15,-5]：橙红段整段落空被删，品红段上沿 180 → 175
        down = strict_profile(RANGES, 0, 0, (-15, -5))
        self.assertEqual(down, [{"lower": [165, 140, 120], "upper": [175, 255, 255]}])
        # 与 S/V 收紧叠加互不干扰
        both = strict_profile(RANGES, 10, 0, (-4, 12))
        self.assertEqual(both[1]["lower"], [176, 150, 120])
        # 什么都不剩 → None；什么都不收紧 → None
        self.assertIsNone(strict_profile(RANGES, 0, 0, (20, 30)))
        self.assertIsNone(strict_profile(RANGES, 0, 0, None))

    def test_hue_cutpoint_lands_between_dot_and_bridge(self):
        """橙红红点(展开 0~4) + 品红背景桥(展开 -11)：刀位落在两簇之间，不钉死在 0。"""
        dot = [self._pil_h(h) for h in (1, 2, 3, 4)] * 30
        bridge = [self._pil_h(180 - 11)] * 40 + [self._pil_h(180 - 10)] * 40
        cut = hue_cutpoint(np.array(dot + bridge), RANGES)
        self.assertTrue(-10 < cut["Otsu"] <= 1, cut)
        self.assertEqual(cut["轴"], [-15, 12])
        self.assertEqual(cut["上侧"], [cut["Otsu"], 12])
        self.assertEqual(cut["下侧"], [-15, cut["Otsu"] - 1])
        # 跨 0/180 的红点(展开 -3~3)是连续的一簇，刀位仍在它与背景之间，不会把它劈开
        straddle = [self._pil_h(h % 180) for h in (-3, -2, -1, 0, 1, 2, 3)] * 20
        cut2 = hue_cutpoint(np.array(straddle + bridge), RANGES)
        self.assertTrue(-10 < cut2["Otsu"] <= -3, cut2)

    def test_hue_cutpoint_refuses_degenerate(self):
        self.assertIsNone(hue_cutpoint(np.full(40, self._pil_h(2)), RANGES))
        self.assertIsNone(hue_cutpoint(np.array([1, 2, 3]), RANGES))

    def test_hue_keep_for_two_sides_and_edges(self):
        axis = (-15, 12)
        self.assertEqual(hue_keep_for("色相上", -4, axis), (-4, 12))
        self.assertEqual(hue_keep_for("色相下", -4, axis), (-15, -5))
        # 刀贴在轴端：一侧整轴(没切)、另一侧为空，都不成档
        self.assertIsNone(hue_keep_for("色相上", -15, axis))
        self.assertIsNone(hue_keep_for("色相下", -15, axis))
        self.assertIsNone(hue_keep_for("饱和", -4, axis))

    def test_direction_cut_keeps_sv_and_adds_hue(self):
        cuts = {"饱和": {"增量": 40}, "亮度": {"增量": 0},
                "色相": {"Otsu": -4, "轴": [-15, 12]}}
        self.assertEqual(direction_cut(cuts, "饱和"),
                         {"delta_s": 40, "delta_v": 0, "hue_cut": None, "hue_keep": None})
        self.assertIsNone(direction_cut(cuts, "亮度"))
        self.assertEqual(direction_cut(cuts, "色相上")["hue_keep"], (-4, 12))
        self.assertEqual(direction_cut(cuts, "色相下")["hue_keep"], (-15, -5))
        self.assertIsNone(direction_cut({"饱和": {"增量": 40}}, "色相上"))

    def test_hue_neighbor_states_step_and_trim(self):
        states = hue_neighbor_states("色相上", -4, (-15, 12))
        self.assertEqual([s["hue_cut"] for s in states], [-6, -4, -2])
        self.assertEqual({s["si"] for s in states}, {30})
        self.assertEqual([s["vi"] for s in states], [0, 1, 2])
        self.assertTrue(all(s["delta_s"] == 0 and s["delta_v"] == 0 for s in states))
        self.assertEqual([s["hue_trim"] for s in states], [9, 11, 13])
        # 越过轴端的陪跑档被剔除
        edge = hue_neighbor_states("色相上", -14, (-15, 12))
        self.assertEqual([s["hue_cut"] for s in edge], [-14, -12])

    def test_stable_winner_prefers_least_hue_trim(self):
        """色相档的 S/V 增量恒为 0，最保守档 = 切掉的轴长最少(保留区间最宽)。"""
        states = hue_neighbor_states("色相上", -4, (-15, 12))
        records = [{"parent_id": 1, "box_local": (25, 7, 18, 17), "state": st,
                    "scan_index": 1} for st in reversed(states)]
        winner, reason, _ = select_stable_winner(records, 2)
        self.assertEqual(reason, "stable_hit")
        self.assertEqual(winner["state"]["hue_cut"], -6)


class LayeredRescueTest(unittest.TestCase):
    """分层救援的纯算法部分：层数开关与子块去重。递归本体在识别器里，靠语料回放验收。"""

    def test_max_depth_defaults_to_single_layer(self):
        """没写 max_depth 的 preset 维持单层 —— 现有 pipeline 行为逐位不变。"""
        cfg, error = normalize_rescue_config({"mode": "active"})
        self.assertIsNone(error)
        self.assertEqual(cfg["max_depth"], 1)
        cfg, error = normalize_rescue_config({"mode": "active", "max_depth": 3})
        self.assertIsNone(error)
        self.assertEqual(cfg["max_depth"], 3)

    def test_max_depth_out_of_range_fails_closed(self):
        for bad in (0, 5):
            cfg, error = normalize_rescue_config({"mode": "active", "max_depth": bad})
            self.assertIsNotNone(error)
            self.assertEqual(cfg["mode"], "off")

    def test_pixel_set_key_ignores_window_offset(self):
        """同一块像素放在不同窗口里，身份相同；换一块像素，身份不同。"""
        a = np.zeros((6, 6), dtype=bool)
        a[1:3, 2:4] = True
        b = np.zeros((10, 10), dtype=bool)
        b[4:6, 3:5] = True      # a 的窗口从 (5,7) 起、b 的从 (4,4) 起，ROI 坐标都是 y8~9 x7~8
        self.assertEqual(pixel_set_key(a, 5, 7), pixel_set_key(b, 4, 4))
        c = a.copy()
        c[1, 2] = False
        self.assertNotEqual(pixel_set_key(a, 5, 7), pixel_set_key(c, 5, 7))


class SameDotTest(unittest.TestCase):
    """不同切法的命中是不是同一个红点：框 IoU 过闸，或围住同一个"!"。"""

    # 2026-10-29 GachaADV_Location1 实测三框；白芯用一小块公共像素代表同一个"!"
    BLOATED = (15, 7, 27, 22)
    CLEAN_S = (25, 7, 18, 16)
    CLEAN_SV = (25, 7, 17, 16)
    CORE = frozenset({(10, 33), (11, 33), (12, 33)})

    def test_shared_core_rescues_low_iou_pair(self):
        """虚胖框对干净框 IoU 0.485，但围住同一个"!" —— 同一处，不是方向歧义。"""
        hits = [(self.BLOATED, self.CORE), (self.CLEAN_S, self.CORE), (self.CLEAN_SV, self.CORE)]
        self.assertFalse(boxes_agree([h[0] for h in hits]))
        self.assertTrue(hits_agree(hits))

    def test_low_iou_without_shared_core_is_still_ambiguous(self):
        other = frozenset({(30, 5), (31, 5)})
        self.assertFalse(hits_agree([(self.BLOATED, other), (self.CLEAN_S, self.CORE)]))

    def test_iou_rule_unchanged_even_without_core(self):
        """IoU 过闸的组合照旧算一致，与白芯无关（原有采纳一个不变）。"""
        self.assertTrue(hits_agree([((25, 6, 18, 18), frozenset()), ((25, 7, 18, 17), frozenset())]))

    def test_bloated_hit_is_excluded_from_adoption(self):
        hits = [(self.BLOATED, self.CORE), (self.CLEAN_S, self.CORE), (self.CLEAN_SV, self.CORE)]
        self.assertEqual(bloated_hits(hits), {0})
        # IoU 过闸的组合谁都不算虚胖
        self.assertEqual(bloated_hits([(self.CLEAN_S, self.CORE), (self.CLEAN_SV, self.CORE)]), set())


if __name__ == "__main__":
    unittest.main()
