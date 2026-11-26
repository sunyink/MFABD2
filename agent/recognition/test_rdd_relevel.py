"""rdd_relevel / rdd_shape 的单测。

纯算法部分不依赖 MaaFramework；末尾「整链」一组要导入识别器本体，环境里没有 maa 时自动跳过。
跑法与 test_rdd_hsv_rescue.py 相同（包根是 agent/，理由见那边的头注）：
    cd agent && python -m unittest recognition.test_rdd_relevel
"""

import unittest
from collections import deque

import numpy as np

try:
    from .rdd_relevel import (
        coarse_levels,
        configured_floor,
        fine_neighbors,
        label_runs,
        level_profile,
        normalize_relevel_config,
        same_spot,
    )
    from .rdd_shape import shape_features, shape_verdict
except ImportError:
    from rdd_relevel import (
        coarse_levels,
        configured_floor,
        fine_neighbors,
        label_runs,
        level_profile,
        normalize_relevel_config,
        same_spot,
    )
    from rdd_shape import shape_features, shape_verdict

try:
    from .binarymatch import RedDotDetector
except Exception:                       # 没有 maa / 不在 agent 包根下
    RedDotDetector = None


RANGES = [{"lower": [0, 140, 120], "upper": [12, 255, 255]},
          {"lower": [165, 140, 120], "upper": [180, 255, 255]}]


def _label_bfs(mask):
    """binarymatch._label_blobs 的照抄，作 label_runs 的对照基准。"""
    h, w = mask.shape
    labeled = np.zeros((h, w), dtype=np.int32)
    label = 0
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or labeled[sy, sx]:
                continue
            label += 1
            queue = deque([(sy, sx)])
            labeled[sy, sx] = label
            while queue:
                y, x = queue.popleft()
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labeled[ny, nx]:
                        labeled[ny, nx] = label
                        queue.append((ny, nx))
    return labeled, label


def _diamond(size):
    half = size // 2
    yy, xx = np.mgrid[0:size, 0:size]
    return (np.abs(xx - half) + np.abs(yy - half)) <= half


class LabelRunsTest(unittest.TestCase):
    def test_matches_bfs_on_random_masks(self):
        rng = np.random.default_rng(20261121)
        for density in (0.15, 0.4, 0.6, 0.85):
            for shape in ((1, 1), (1, 17), (17, 1), (23, 31), (40, 9)):
                mask = rng.random(shape) < density
                got, n_got = label_runs(mask)
                want, n_want = _label_bfs(mask)
                self.assertEqual(n_got, n_want)
                self.assertTrue(np.array_equal(got, want), (density, shape))

    def test_u_shape_joins_through_lower_row(self):
        mask = np.array([[1, 0, 1],
                         [1, 0, 1],
                         [1, 1, 1]], dtype=bool)
        labeled, n = label_runs(mask)
        self.assertEqual(n, 1)
        self.assertTrue(np.array_equal(labeled, mask.astype(np.int32)))

    def test_empty_and_row_gap(self):
        self.assertEqual(label_runs(np.zeros((4, 5), dtype=bool))[1], 0)
        mask = np.zeros((3, 4), dtype=bool)
        mask[0, 1] = mask[2, 1] = True      # 隔一行，不相连
        self.assertEqual(label_runs(mask)[1], 2)


class ConfigTest(unittest.TestCase):
    def test_default_is_off(self):
        cfg, error = normalize_relevel_config(None)
        self.assertIsNone(error)
        self.assertEqual(cfg["mode"], "off")

    def test_invalid_falls_back_to_off(self):
        for raw in ("active", {"mode": "on"}, {"mode": "active", "step": 5},
                    {"mode": "active", "s_min": 200, "s_max": 100},
                    {"mode": "active", "s_min": 0, "s_max": 255, "step": 2},
                    {"mode": "active", "time_budget_ms": 0}):
            cfg, error = normalize_relevel_config(raw)
            self.assertIsNotNone(error, raw)
            self.assertEqual(cfg["mode"], "off", raw)

    def test_levels_nearest_first_and_skip_configured(self):
        cfg, _ = normalize_relevel_config({"mode": "active"})
        levels = coarse_levels(cfg, 140)
        self.assertEqual(levels[:4], [130, 150, 120, 160])
        self.assertNotIn(140, levels)
        self.assertEqual((min(levels), max(levels), len(levels)), (60, 220, 16))

    def test_fine_neighbors_try_configured_side_first(self):
        cfg, _ = normalize_relevel_config({"mode": "active"})
        self.assertEqual(fine_neighbors(cfg, 140, 130), [135, 125])
        self.assertEqual(fine_neighbors(cfg, 140, 150), [145, 155])
        self.assertEqual(fine_neighbors(cfg, 140, 60), [65])        # 55 出了范围
        self.assertEqual(fine_neighbors(cfg, 135, 130), [125])      # 135 是配置档，不重跑

    def test_profile_only_moves_saturation_floor(self):
        self.assertEqual(configured_floor(RANGES), 140)
        self.assertEqual(level_profile(RANGES, 100), [
            {"lower": [0, 100, 120], "upper": [12, 255, 255]},
            {"lower": [165, 100, 120], "upper": [180, 255, 255]}])


class ShapeGateTest(unittest.TestCase):
    def _dot(self, size=13):
        blob = _diamond(size)
        inner = np.zeros_like(blob)
        mid = size // 2
        inner[3:size - 5, mid] = True       # 竖条
        inner[size - 4, mid] = True         # 点
        return blob & ~inner, inner

    def test_diamond_with_centred_core_passes(self):
        for size in (9, 11, 13, 17):
            blob, inner = self._dot(size)
            ok, blocked = shape_verdict(shape_features(blob, inner))
            self.assertTrue(ok, (size, blocked))

    def test_square_block_is_not_a_diamond(self):
        blob = np.ones((13, 13), dtype=bool)
        inner = np.zeros_like(blob)
        inner[3:10, 6] = True
        self.assertEqual(shape_verdict(shape_features(blob & ~inner, inner)), (False, "菱形"))

    def test_dot_dragging_a_card_edge_is_rejected(self):
        """红点左边连着一条卡片红边：框被拉宽，菱形不贴合。"""
        blob, inner = self._dot(13)
        wide = np.zeros((13, 27), dtype=bool)
        wide[:, 14:] = blob
        wide[0:3, :14] = True
        wide_inner = np.zeros_like(wide)
        wide_inner[:, 14:] = inner
        ok, _ = shape_verdict(shape_features(wide, wide_inner))
        self.assertFalse(ok)

    def test_off_centre_core_is_rejected(self):
        blob = _diamond(13)
        inner = np.zeros_like(blob)
        inner[4:9, 3] = True                # 白芯偏到左边
        self.assertEqual(shape_verdict(shape_features(blob & ~inner, inner)), (False, "居中"))

    def test_no_core_is_rejected(self):
        blob = _diamond(13)
        self.assertEqual(shape_verdict(shape_features(blob, np.zeros_like(blob))),
                         (False, "居中"))


class SameSpotTest(unittest.TestCase):
    def test_shared_core_or_overlapping_box(self):
        a = {"box": (10, 10, 12, 12), "core": frozenset({(15, 16)})}
        b = {"box": (0, 10, 26, 13), "core": frozenset({(15, 16), (16, 16)})}   # 框偏大但同芯
        c = {"box": (11, 10, 12, 12), "core": frozenset({(40, 40)})}            # 不同芯但框重叠
        d = {"box": (40, 40, 12, 12), "core": frozenset({(45, 46)})}
        self.assertTrue(same_spot(a, b))
        self.assertTrue(same_spot(a, c))
        self.assertFalse(same_spot(a, d))


@unittest.skipIf(RedDotDetector is None, "需要 maa 与 agent 包根")
class WholeChainTest(unittest.TestCase):
    """合成一颗红点（Pillow HSV，H 0-255），验换档的进出口。"""

    AREA, ASPECT, GAP, CONF = (30, 1200), (0.6, 1.6), 0.35, 0.55

    def _scene(self, hue, sat):
        hsv = np.zeros((30, 32, 3), dtype=np.uint8)
        hsv[...] = (150, 140, 85)                       # 蓝底
        blob = _diamond(13)
        inner = np.zeros_like(blob)
        inner[3:8, 6] = True
        inner[9, 6] = True
        patch = hsv[8:21, 10:23]
        patch[blob] = (hue, sat, 205)
        patch[inner] = (0, 20, 250)                     # 白色感叹号
        return hsv

    def _relevel(self, hsv, **override):
        cfg, error = normalize_relevel_config({"mode": "active", **override})
        self.assertIsNone(error)
        return RedDotDetector()._run_relevel(
            hsv_np=hsv, hsv_ranges=RANGES, area_range=self.AREA, aspect_range=self.ASPECT,
            gap_ratio=self.GAP, min_conf=self.CONF, rx=0, ry=0, config=cfg)

    def _baseline_hit(self, hsv):
        return RedDotDetector()._detect_once(
            hsv, RANGES, self.AREA[0], self.AREA[1], self.ASPECT[0], self.ASPECT[1],
            self.GAP, self.CONF, 0, 0)["hit"]

    def test_washed_out_dot_is_recovered_below_configured_level(self):
        hsv = self._scene(hue=0, sat=125)
        self.assertFalse(self._baseline_hit(hsv))
        result = self._relevel(hsv)
        self.assertEqual(result["_decision"], "stable_hit")
        self.assertEqual(result["胜出"]["档"], 120)
        self.assertEqual(result["胜出"]["复现档"], 125)
        self.assertEqual(result["胜出"]["框"], [10, 8, 13, 13])
        confirmed = RedDotDetector()._relevel_confirm_full(
            hsv_np=hsv, winner=result["_winner"], area_range=self.AREA,
            aspect_range=self.ASPECT, gap_ratio=self.GAP, min_conf=self.CONF, rx=0, ry=0)
        self.assertIsNotNone(confirmed)

    def test_yellow_dot_never_enters(self):
        """同样的形状换成黄色（OpenCV 色相约 20，Pillow 约 28），哪一档都不认。"""
        for sat in (125, 200):
            result = self._relevel(self._scene(hue=28, sat=sat))
            self.assertEqual(result["_decision"], "no_hit")
            self.assertEqual(result["停因"], "最松档没有够大的红块")

    def test_single_level_hit_is_not_enough(self):
        """只在孤零零一档认得出的不采纳：把范围收窄到只剩 120 这一档及其够不着的邻档。"""
        hsv = self._scene(hue=0, sat=121)       # 120 认得出，125 / 115 中只有 115 认得出
        self.assertEqual(self._relevel(hsv)["_decision"], "stable_hit")
        result = self._relevel(hsv, s_min=120, s_max=220)   # 115 出了范围，125 不够红
        self.assertEqual(result["_decision"], "no_hit")

    def test_nothing_red_stops_at_first_check(self):
        hsv = np.zeros((30, 32, 3), dtype=np.uint8)
        hsv[...] = (150, 140, 85)
        result = self._relevel(hsv)
        self.assertEqual((result["窗口"], result["重跑"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
