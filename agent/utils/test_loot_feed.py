"""loot_feed 的纯算法单测：用程序画的合成画面，不依赖 MaaFramework 与游戏素材。

跑法（都从仓库根执行）：
    python agent/utils/test_loot_feed.py
    python -m unittest discover -s agent/utils -p 'test_loot_feed.py'

合成画面照实测行为画：新条目从第 0 格进来、其余瞬间下移一格；到达后底框闪白 0.4 秒，
2.1 秒起淡出，3.1 秒消失。真实截图与录像的回放在 priv doc/agent/loot_feed/verify/。
"""

import unittest

import numpy as np

try:
    from . import loot_feed as lf
except ImportError:      # 直接 python agent/utils/test_loot_feed.py
    import loot_feed as lf

LAY = lf.LAYOUTS["adb"]
GLOW, FADE, GONE = 0.4, 2.1, 3.1
STEP = 0.09


def _icon(item):
    """每种物品一张 6×6 色块图标，放大后填进底框内部。"""
    rng = np.random.default_rng(1000 + item)
    return rng.integers(0, 256, size=(6, 6, 3), dtype=np.uint8)


def _text(item, count):
    """文字条：用亮块充当字，块数与位置随物品和数量变化（只要像素不同即可）。"""
    rng = np.random.default_rng(item * 7919 + count)
    strip = np.zeros((20, 120), dtype=np.uint8)
    for i in range(8):
        if rng.random() < 0.8:
            strip[int(rng.integers(0, 6)):int(rng.integers(12, 20)), i * 14 + 2:i * 14 + 11] = 255
    strip[4:16, 2:8] = 255
    return strip


def render(events, t, frozen_at=None):
    """events: [(到达时刻, 物品号, 数量)]。返回 t 时刻的 720p 画面。"""
    if frozen_at is not None and t > frozen_at:
        t = frozen_at
    rng = np.random.default_rng(7)
    img = rng.integers(0, 60, size=(720, 1280, 3), dtype=np.uint8)
    visible = [e for e in events if 0 <= t - e[0] < GONE]
    visible.sort(key=lambda e: -e[0])
    box = np.array(LAY.box_bgr, dtype=np.float32)
    for slot, (t_arrive, item, count) in enumerate(visible):
        if slot >= LAY.slots:
            break
        age = t - t_arrive
        y0, x0, w = LAY.row_top(slot), LAY.box_x0, LAY.box_w
        if y0 + w > 720:
            break
        patch = np.empty((w, w, 3), dtype=np.float32)
        patch[:] = box
        patch[6:w - 6, 6:w - 6] = np.kron(_icon(item), np.ones((6, 6, 1)))[:w - 12, :w - 12]
        text = np.zeros((w, 140, 3), dtype=np.float32)
        text[10:30, 8:128] = _text(item, count)[:, :, None]
        if age < GLOW:
            patch[:] = (251, 224, 190)
        alpha = 1.0 if age < FADE else max(0.0, 1 - (age - FADE) / (GONE - FADE))
        for dst, src in ((img[y0:y0 + w, x0:x0 + w], patch), (img[y0:y0 + w, LAY.text_x0:LAY.text_x0 + 140], text)):
            dst[:] = (dst * (1 - alpha) + src * alpha).astype(np.uint8)
    return img


def track(events, until, step=STEP, windows=None, frozen_at=None):
    """windows=(窗口秒, 空档秒) 时只在窗口内喂帧。"""
    tr, t = lf.FeedTracker(LAY), 0.0
    while t < until:
        if windows is None or (t % (windows[0] + windows[1])) < windows[0]:
            tr.feed(t, render(events, t, frozen_at))
        t += step
    return tr


def items_of(tracker, events):
    """按图标指纹把跟踪到的条目对回物品号。"""
    ref = {}
    for _, item, _ in events:
        patch = np.kron(_icon(item), np.ones((6, 6, 1)))[:LAY.box_w - 12, :LAY.box_w - 12]
        inner = np.empty((LAY.box_w - 8, LAY.box_w - 8, 3), dtype=np.float32)
        inner[:] = LAY.box_bgr
        inner[2:-2, 2:-2] = patch
        ref[item] = lf._signature(inner.astype(np.uint8))
    out = []
    for e in tracker.ordered():
        out.append(None if e.sig is None else min(ref, key=lambda k: lf.sig_distance(e.sig, ref[k])))
    return out


class TrackerTest(unittest.TestCase):
    def test_burst_longer_than_screen(self):
        events = [(0.5 + 0.2 * i, i, 10 + i) for i in range(14)]
        tr = track(events, 7.0)
        self.assertEqual(items_of(tr, events), list(range(14)))
        self.assertFalse(tr.notes)

    def test_sparse_sampling(self):
        events = [(0.5 + 0.2 * i, i, 10 + i) for i in range(10)]
        for step in (0.15, 0.2, 0.3):
            self.assertEqual(items_of(track(events, 6.0, step=step), events), list(range(10)), step)

    def test_same_icon_back_to_back(self):
        # 同屏两条同图标、数量不同：按格位算两条
        events = [(0.5, 1, 21000), (0.7, 2, 5), (0.9, 1, 28000)]
        self.assertEqual(items_of(track(events, 5.0), events), [1, 2, 1])

    def test_same_icon_after_previous_faded(self):
        # 前一条淡出期间，同图标的新条目到了：靠到达闪光认出是新的
        for gap in (1.8, 2.2, 2.6, 3.5):
            events = [(0.5, 1, 100), (0.5 + gap, 1, 100)]
            self.assertEqual(len(track(events, 0.5 + gap + 4.0).entries), 2, gap)

    def test_gaps_between_watch_windows(self):
        events = [(0.6 + 0.2 * i, i, 10 + i) for i in range(9)] + [(4.9, 20, 1), (7.6, 21, 2), (7.8, 22, 3)]
        tr = track(events, 12.0, windows=(2.0, 0.7))
        self.assertEqual(items_of(tr, events), list(range(9)) + [20, 21, 22])

    def test_frozen_picture_is_not_new(self):
        # 画面在 1.6 秒处卡住（条目赖在屏上远超正常寿命），中间还有观察空档：不重复计数
        events = [(0.5, 1, 100), (0.7, 2, 200), (0.9, 3, 300)]
        tr = track(events, 10.0, windows=(2.0, 0.7), frozen_at=1.6)
        self.assertEqual(items_of(tr, events), [1, 2, 3])

    def test_busy_then_quiet(self):
        events = [(0.5, 1, 100), (0.7, 2, 200)]
        tr, t, seen_busy = lf.FeedTracker(LAY), 0.0, False
        while t < 3.0:
            tr.feed(t, render(events, t))
            seen_busy = seen_busy or tr.busy(t)
            t += STEP
        self.assertTrue(seen_busy)
        self.assertFalse(tr.busy(t))
        self.assertTrue(all(len(e.crops) >= 3 for e in tr.entries))


class ReadTest(unittest.TestCase):
    def test_parse_row(self):
        self.assertEqual(lf.parse_row("水果罐头×1008"), ("水果罐头", 1008))
        self.assertEqual(lf.parse_row(" 金币 x 360,000 "), ("金币", 360000))
        self.assertEqual(lf.parse_row("小麥×312"), ("小麥", 312))
        for bad in ("巧克力×224+", "雷瓦", "×12", "豆756", "金币×", ""):
            self.assertIsNone(lf.parse_row(bad), bad)

    def test_split_line(self):
        self.assertEqual(lf.split_line("原木×128@金币×9", 2), ["原木×128", "金币×9"])
        self.assertEqual(lf.split_line("原木×128＠金币×9", 2), ["原木×128", "金币×9"])   # 全角分隔符
        self.assertIsNone(lf.split_line("原木×128金币×9", 2))

    def test_right_end_cuts_far_bright_spot(self):
        strip = np.zeros((47, 280, 3), dtype=np.uint8)
        strip[12:36, 5:100] = 255
        strip[:, 180:186] = 255          # 字右边隔开一段的特效亮斑
        self.assertEqual(lf.text_right_end(strip), 106)
        self.assertEqual(lf.text_right_end(strip, max_gap=0), 192)

    def test_pack_lines(self):
        strips = []
        for width in (100, 120, 90):
            s = np.zeros((47, 280, 3), dtype=np.uint8)
            s[12:36, 5:width] = 255
            strips.append(s)
        img, lines = lf.pack_lines(strips)
        self.assertEqual(len(lines), 1)
        roi, members = lines[0]
        self.assertEqual(members, [0, 1, 2])
        self.assertEqual(roi[:2] + roi[3:], [0, 0, 47])
        self.assertEqual(roi[2], sum(w + 6 for w in (100, 120, 90)) + 2 * lf._sep_image(47).shape[1])
        self.assertFalse(img[:, roi[2]:].any())
        # 放不下就换行
        _, lines = lf.pack_lines(strips * 6)
        self.assertGreater(len(lines), 1)
        self.assertEqual(sum(len(m) for _, m in lines), 18)

    def test_median_removes_passing_overlay(self):
        tr = lf.FeedTracker(LAY)
        e = lf.Entry(idx=0, t_first=0.0)
        base = np.zeros((47, 280, 3), dtype=np.uint8)
        base[12:36, 5:100] = 255
        for i in range(3):
            c = base.copy()
            c[:, 120 + 30 * i:126 + 30 * i] = 255      # 每张上位置不同的亮斑
            e.crops.append((0.5 + 0.3 * i, i, c))
        self.assertTrue(np.array_equal(tr.clean_strip(e), base))


class LayoutTest(unittest.TestCase):
    def test_layout_from(self):
        self.assertEqual(lf.layout_from(None), lf.LAYOUTS["adb"])
        self.assertEqual(lf.layout_from({"base": "pc"}), lf.LAYOUTS["pc"])
        lay = lf.layout_from({"base": "pc", "tol": 12, "box_bgr": [107, 84, 47], "nonsense": 1})
        self.assertEqual((lay.tol, lay.box_bgr, lay.pitch), (12, (107, 84, 47), lf.LAYOUTS["pc"].pitch))


class SessionTest(unittest.TestCase):
    def test_pending_follows_casts_and_entries(self):
        lf._SESSIONS.clear()
        self.assertFalse(lf.has_pending(1, "x"))
        s = lf.open_session(1, "x", lambda: LAY)
        self.assertFalse(lf.has_pending(1, "x"))
        s.casts.append((0.0, "吸取"))
        self.assertTrue(lf.has_pending(1, "x"))
        s.casts.clear()
        events = [(0.1, 1, 5)]
        for i in range(12):
            s.tracker.feed(i * STEP, render(events, i * STEP))
        self.assertTrue(lf.has_pending(1, "x"))
        for e in s.tracker.entries:
            e.taken = True
        self.assertFalse(lf.has_pending(1, "x"))
        lf.open_session(2, "x", lambda: LAY)          # 换任务：旧任务的状态丢弃
        self.assertIsNone(lf.peek_session(1, "x"))


if __name__ == "__main__":
    unittest.main()
