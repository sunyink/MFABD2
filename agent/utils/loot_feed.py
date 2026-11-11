"""掉落播报（箱庭左侧「获得物品」滚动列表）的读取：像素层跟踪 + 文字条合成拼行。

只用 numpy，不碰 MaaFw，可以拿截图离线回放。分三步，贵的 OCR 只在最后做一次：

1. 跟踪（每帧约 1 毫秒）：每条左侧有一个不透明的图标底框，颜色恒定。只认底框是纯色的行，
   取图标指纹，与上一帧按整列对齐求下移格数，最上面多出的格就是新条目。
2. 合成：同一条在不同时刻留下的文字条逐像素取中位数。字不动，技能特效和路过的角色在动，
   只在个别时刻盖上来的亮斑被滤掉。
3. 拼行：把各条裁到字的右端，横向拼成一行、之间画分隔符。识别一行的耗时只跟行宽有关，
   拼起来读比逐条读省一半以上。

依据的游戏行为（2026-11 两端实测）：
- 新条目只从最上面一格进来，其余瞬间下移一格（没有滑动过程），所以行恒在固定格点上。
- 条目只会从底部离开：到达约 2.1 秒后开始淡出，或被挤出屏幕。
- 刚到的条目底框闪白约 0.4 秒；到达动画的大箭头会短暂盖住最上面两格的底框。
- 名字跨次会重复（同屏两条金币），所以不能按名字去重，只能按格位对齐。
"""

import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Layout:
    box_x0: int       # 图标底框左边 x
    box_w: int        # 底框边长
    cy0: float        # 第 0 格底框中线 y
    pitch: float      # 格距
    slots: int        # 屏内底框完整可见的格数
    text_x0: int      # 文字条左右
    text_x1: int
    tol: int = 4      # 判为底框色的单像素容差（无损截图下纯色行误差为 0–1）
    mean_tol: float = 3.0            # 八小段合起来的平均色容差：到达闪光的残留是整体偏色，靠它排除
    box_bgr: tuple = (111, 85, 50)   # 底框颜色，两端一致，稀有物品也一样

    def row_top(self, slot):
        return int(round(self.cy0 - self.box_w / 2.0 + self.pitch * slot))


LAYOUTS = {
    "adb": Layout(box_x0=151, box_w=43, cy0=328.0, pitch=46.3, slots=9, text_x0=198, text_x1=480),
    "pc": Layout(box_x0=126, box_w=36, cy0=333.5, pitch=38.8, slots=10, text_x0=166, text_x1=400),
}


def layout_from(config):
    """节点 attach → Layout。`base` 选一套内置版式，其余键逐项覆盖。"""
    config = dict(config or {})
    base = LAYOUTS.get(str(config.pop("base", "adb")), LAYOUTS["adb"])
    known = {k: v for k, v in config.items() if k in Layout.__dataclass_fields__}
    if "box_bgr" in known:
        known["box_bgr"] = tuple(known["box_bgr"])
    return Layout(**{**base.__dict__, **known})


SOLID_MAX = 2.6   # 条目到达这么久以后不可能还是纯色（实测 2.05–2.15 秒后开始淡出）
FRESH = 0.35           # 条目登记后这么久内若只见过一帧，那一帧的指纹不当硬证据
CONT_GAP = 0.35   # 上次看到纯色不超过这么久，就认它还在（不管年龄）
SIG_EXACT = 1.5   # 同一格原样没动时的指纹差（无损截图下为 0）
SIG_GOOD = 4.5    # 不超过它算同一图标（同名实测最大 2.6，沾了到达闪光时到 3.9）
SIG_BAD = 9.0     # 超过它算明确不是同一图标（异名实测最小 4.9，多数在 10 以上）
CROP_AGES = (0.5, 0.8, 1.1, 1.4, 1.7)   # 每条在这些年龄附近各留一张文字条
STRIP_SAME = 2.0  # 两张文字条的平均像素差不超过它，算画面原样没动（字和底图都一样）
BUSY_WINDOW = 0.35   # 这么久内来过新条目或见过到达闪光，就算列表还在出条（出条间隔 0.2 秒）
YOUNG = 1.0          # 已知存在但还没看清的条目，在这个年龄内值得再等


@dataclass
class Row:
    slot: int
    y_top: int
    sig: np.ndarray


@dataclass
class Entry:
    idx: int
    t_first: float                 # 首次确认存在的帧时刻（真实到达时刻不晚于它）
    seq: float = 0.0               # 到达先后（补登记的条目排在它上面那条之前）
    sig: np.ndarray | None = None
    t_last: float | None = None    # 最近一次看到它是纯色
    seen: int = 0
    sig_changed: int = 0           # 位置对得上但图标指纹变了的次数（被挡或沾了闪光）
    crops: list = field(default_factory=list)   # [(年龄, 档位, 文字条)]
    taken: bool = False            # 已交给上层读过
    last_slot: int = -1            # 最近一次纯色时所在的格与那时的文字条，用来认出「画面卡住」
    last_strip: np.ndarray | None = None

    def age(self, t):
        return t - self.t_first


def _is_box(strip, layout):
    return np.abs(strip.astype(np.int16) - np.array(layout.box_bgr, dtype=np.int16)).max(axis=-1) <= layout.tol


def _solid_at(img, layout, y0):
    """底框四条边上靠近四角的 8 小段是否都是底框色。

    只看角附近：大图标（远古水晶）会贴到左右边中段，右上角内侧还有稀有度小标记。
    """
    x0, w = layout.box_x0, layout.box_w
    a, b = 7, 12
    segs = (
        img[y0 + 2:y0 + 4, x0 + a:x0 + b], img[y0 + 2:y0 + 4, x0 + w - b:x0 + w - a],
        img[y0 + w - 4:y0 + w - 2, x0 + a:x0 + b], img[y0 + w - 4:y0 + w - 2, x0 + w - b:x0 + w - a],
        img[y0 + a:y0 + b, x0 + 2:x0 + 4], img[y0 + w - b:y0 + w - a, x0 + 2:x0 + 4],
        img[y0 + a:y0 + b, x0 + w - 4:x0 + w - 2], img[y0 + w - b:y0 + w - a, x0 + w - 4:x0 + w - 2],
    )
    if not all(_is_box(seg, layout).mean() >= 0.9 for seg in segs):
        return False
    mean = np.concatenate([seg.reshape(-1, 3) for seg in segs]).mean(axis=0)
    return bool(np.abs(mean - np.array(layout.box_bgr, dtype=np.float32)).max() <= layout.mean_tol)


def _signature(inner, n=3):
    """图标指纹：底框内部按 n×n 分块取平均色。粗分块才不受半个像素的渲染错位影响。"""
    h, w = inner.shape[:2]
    ys = np.linspace(0, h, n + 1).round().astype(int)
    xs = np.linspace(0, w, n + 1).round().astype(int)
    out = np.empty((n, n, 3), dtype=np.float32)
    for i in range(n):
        for j in range(n):
            out[i, j] = inner[ys[i]:ys[i + 1], xs[j]:xs[j + 1]].reshape(-1, 3).mean(axis=0)
    return out


def sig_distance(a, b):
    return float(np.abs(a - b).mean())


def detect_rows(img, layout):
    """这一帧里所有纯色底框的行，自上而下。"""
    rows = []
    for slot in range(layout.slots):
        base = layout.row_top(slot)
        for y0 in (base, base - 1, base + 1):
            if y0 < 0 or y0 + layout.box_w > img.shape[0]:
                continue
            if _solid_at(img, layout, y0):
                x0, w = layout.box_x0, layout.box_w
                rows.append(Row(slot=slot, y_top=y0, sig=_signature(img[y0 + 4:y0 + w - 4, x0 + 4:x0 + w - 4])))
                break
    return rows


def glow_at_top(img, layout):
    """第 0 格的底框是否正处在到达闪光里（亮的蓝白色）。只当「来过新条目」的旁证用。"""
    y0, x0, w = layout.row_top(0), layout.box_x0, layout.box_w
    if y0 < 0 or y0 + w > img.shape[0]:
        return False
    px = np.concatenate([img[y0 + 8:y0 + w - 8, x0 + 2:x0 + 4].reshape(-1, 3),
                         img[y0 + 8:y0 + w - 8, x0 + w - 4:x0 + w - 2].reshape(-1, 3)]).astype(np.int16)
    b, g, r = px[:, 0], px[:, 1], px[:, 2]
    return bool(((b >= 195) & (g >= 165) & (r >= 125) & (b - r >= 20)).mean() >= 0.8)


class FeedTracker:
    """逐帧喂截图（BGR），攒出按到达先后排列的条目。时间 t 用秒，单调递增即可。"""

    def __init__(self, layout):
        self.layout = layout
        self.entries = []      # 全部条目，按登记先后；按到达先后看 ordered()
        self.stack = []        # 当前自上而下、还可能是纯色的条目（stack[0] 在第 0 格）
        self.t_last_new = None
        self.t_last_glow = None
        self._glow_open = None  # 见过到达闪光、还没对应到新条目的时刻
        self._grave = []        # 刚从列表底部清出的条目：画面卡住时要把它们认回来
        self._seq = 0
        self.frames = 0
        self.notes = []        # [(t, 类别, 说明)]：resync 重同步、late 补登记、frozen 画面卡住认回

    def _alive(self, e, t):
        return e.age(t) <= SOLID_MAX or (e.t_last is not None and t - e.t_last <= CONT_GAP)

    def _new_entry(self, t, seq):
        e = Entry(idx=len(self.entries), t_first=t, seq=seq)
        self.entries.append(e)
        return e

    def _shift(self, t, rows):
        """这一帧相对已知列表下移了几格。返回 (k, 对上的行数)；整列都解释不了返回 None。

        只用「看到了纯色行」这种正面证据，不拿「没看到」当证据；按整列投票，
        个别行对不上（被特效挡住、指纹沾了闪光）不推翻整列的结论。
        """
        s_bot = rows[-1].slot
        # 栈顶条目上次纯色之后又见过到达闪光 → 这期间一定来过新条目（防同图标的新条目并进旧条目）
        top = self.stack[0] if self.stack else None
        k_min = 1 if (top is not None and top.sig is not None and top.t_last is not None
                      and self._glow_open is not None and self._glow_open > top.t_last) else 0
        best, weak, fallback = None, None, None
        for k in range(k_min, s_bot + 2):
            good = bad = out = exact = below = unsure = unsure_bad = 0
            for r in rows:
                i = r.slot - k
                if i < 0:
                    continue
                if i >= len(self.stack):
                    out += 1           # 落在已知列表下方：这个 k 解释不了它
                    below += i == len(self.stack) + below      # 其中紧贴列表底部、连成一串的
                    continue
                e = self.stack[i]
                if e.sig is None:
                    continue           # 还没看清的条目：通配
                d = sig_distance(r.sig, e.sig)
                fresh = e.seen <= 1 and t - e.t_first <= FRESH and d > SIG_GOOD
                unsure += fresh        # 只见过一帧的新条目：那一帧的指纹可能还沾着到达动画，对不上不算数
                if d > SIG_BAD or not self._alive(e, t):
                    bad += 1           # 图标不同；或这条早该淡出了，图标相同也不是它
                    unsure_bad += fresh
                elif d <= SIG_GOOD:
                    good += 1
                    exact += d <= SIG_EXACT
            # 对上的要比对不上的多；同格原样没动的行是最硬的证据，有它时平票也认
            if good and (good > bad + out or (exact and good >= bad + out)):
                key = (-(good - bad - out), -good, k)
                if best is None or key < best[0]:
                    best = (key, k, good)
            elif below and out == below and bad == unsure_bad and (unsure or good > bad):
                # 说不通的只有两样：紧贴列表底部多出来的行，和只见过一帧的新条目指纹对不上。
                # 到达箭头会盖住上面两格，两条到齐后常常先露出第 0 格（还带着动画余色）、下一帧才露出第 1 格。
                # 多出来的行按「来得更早、之前没看清」补登记，不为它把整列判成全新（11-10 实跑：金币×73500 记了两条）
                key = (below, k)
                if weak is None or key < weak[0]:
                    weak = (key, k, max(good, 1))
            elif not bad and not out and fallback is None:
                fallback = k           # 没有硬证据但也不矛盾：取新条目最少的解释
        if best is not None:
            return best[1], best[2]
        if weak is not None:
            return weak[1], weak[2]
        if fallback is not None:
            return fallback, 0
        return None

    def _strip(self, img, r):
        lay = self.layout
        return img[max(0, r.y_top - 2):r.y_top + lay.box_w + 2, lay.text_x0:lay.text_x1]

    def _revive(self, t, img, rows):
        """画面卡住（游戏卡顿、截图返回旧帧）时，超龄的条目其实还原样留在屏上。

        条件收得很紧：这期间没见过到达闪光，并且每一行都在原来那一格、图标与文字条像素都没变。
        只要有一行对不上就不认，照常按新条目处理。
        """
        if not self._grave:
            return
        back, n = [], len(self.stack)
        for r in rows:
            if r.slot < n:
                e = self.stack[r.slot]
                if e.sig is None or sig_distance(r.sig, e.sig) > SIG_EXACT:
                    return
                continue
            g = next((g for g in self._grave if g.last_slot == r.slot), None)
            if (g is None or r.slot != n + len(back) or sig_distance(r.sig, g.sig) > SIG_EXACT
                    or (self.t_last_glow is not None and self.t_last_glow > g.t_last)
                    or g.last_strip is None or g.last_strip.shape != self._strip(img, r).shape
                    or np.abs(g.last_strip.astype(np.int16) - self._strip(img, r)).mean() > STRIP_SAME):
                return
            back.append(g)
        for g in back:
            self._grave.remove(g)
            g.t_last = t               # 此刻又看到它是纯色，后面的对齐才会认它
            self.stack.append(g)
        if back:
            self.notes.append((t, "frozen", f"画面未变，认回 {len(back)} 条"))

    def feed(self, t, img):
        """喂一帧，返回这一帧新登记的条目数。"""
        self.frames += 1
        if glow_at_top(img, self.layout):
            self.t_last_glow = self._glow_open = t
        # 条目只会从底部离场：不可能再是纯色的从底部清出
        while self.stack and not self._alive(self.stack[-1], t):
            gone = self.stack.pop()
            if gone.sig is not None:
                self._grave.insert(0, gone)
        del self._grave[12:]
        rows = detect_rows(img, self.layout)
        if not rows:
            return 0
        self._revive(t, img, rows)
        found = self._shift(t, rows)
        if found is None:
            self.notes.append((t, "resync", f"对不上已知列表，当前 {rows[-1].slot + 1} 格都算新的"))
            self.stack = []
            k, good = rows[-1].slot + 1, 0
        else:
            k, good = found
        for _ in range(k):
            self._seq += 1
            self.stack.insert(0, self._new_entry(t, self._seq))
        added = k
        if k:
            self.t_last_new = t
            self._glow_open = None
            self._grave.clear()        # 列表动过了，旧画面不可能原样还在
        for r in rows:
            if r.slot > len(self.stack):
                continue               # 与列表不相连的纯色行（多半是场景撞色）
            if r.slot == len(self.stack):
                if not good:
                    continue
                # 紧贴列表底部多出一条：它比上面那条来得早，只是之前一直没被看清
                above = self.stack[-1]
                self.stack.append(self._new_entry(above.t_first, above.seq - 0.5))
                self.notes.append((t, "late", f"补登记第 {r.slot} 格"))
                self.t_last_new = t
                added += 1
            e = self.stack[r.slot]
            if e.sig is not None and sig_distance(r.sig, e.sig) > SIG_BAD:
                if not good:
                    continue           # 整列没有旁证时，不采信与原指纹明显不符的行
                e.sig_changed += 1
            e.sig = r.sig
            e.t_last = t
            e.seen += 1
            e.last_slot, e.last_strip = r.slot, self._strip(img, r).copy()
            if not e.taken:
                self._keep_crop(e, t)
        del self.stack[16:]
        return added

    def _keep_crop(self, e, t):
        age = e.age(t)
        want = min(range(len(CROP_AGES)), key=lambda i: abs(CROP_AGES[i] - age))
        for i, (a, w, _) in enumerate(e.crops):
            if w == want:
                if abs(age - CROP_AGES[want]) < abs(a - CROP_AGES[want]):
                    e.crops[i] = (age, want, e.last_strip)
                return
        e.crops.append((age, want, e.last_strip))

    def ordered(self):
        return sorted(self.entries, key=lambda e: e.seq)

    def busy(self, t):
        """列表是否还在出条，或有刚到还没看清的条目。"""
        if self.t_last_glow is not None and t - self.t_last_glow <= BUSY_WINDOW:
            return True
        if self.t_last_new is not None and t - self.t_last_new <= BUSY_WINDOW:
            return True
        return any(e.sig is None and e.age(t) <= YOUNG for e in self.stack)

    def unsettled(self, t, min_crops=2):
        """还在屏上、文字条攒得不够的条目数：再多看几帧能让合成更干净。"""
        return sum(1 for e in self.stack
                   if not e.taken and e.sig is not None and len(e.crops) < min_crops and e.age(t) <= YOUNG)

    def raw_strip(self, entry):
        """年龄居中的那张原始文字条（到达动画已过、还没淡出）。"""
        if not entry.crops:
            return None
        mid = CROP_AGES[len(CROP_AGES) // 2]
        return min(entry.crops, key=lambda c: abs(CROP_AGES[c[1]] - mid))[2]

    def clean_strip(self, entry):
        """各时刻的文字条逐像素取中位数；只有两张时取较暗的像素。"""
        crops = [c for _, _, c in entry.crops]
        if not crops:
            return None
        h = min(c.shape[0] for c in crops)
        stack = np.stack([c[:h] for c in crops])
        if len(crops) >= 3:
            return np.median(stack, axis=0).astype(np.uint8)
        return stack.min(axis=0)


# ---- 拼行 ----

SEP = "@"   # 物品名里不会出现；项目 OCR 实测读得稳（#、+、| 都有读错或吞掉的）

# 「@」的灰度点阵（22 行 × 20 列，十六进制）。与实测通过的那张一致，不依赖字体库。
_SEP_GLYPH = (
    "0000000000000000113521030000000000000000",
    "00000000036cbae5fffffffadbb8590100000000",
    "00000035cdffffffffffffffffffffc433000000",
    "00004dfdfff9b27e491c345d86b4fefffb370000",
    "0017e9fff34600000000000000005ff9ffd60800",
    "00b0ffee37000001272c02000d090042f9ff8100",
    "13fdff8b000039e4ffffe87dfef40d00beffcc00",
    "51ffff39001df7ffffffffffffff18007afffa09",
    "92ffe5020091fffa8b2f83f6ffff180036ffff39",
    "b2ffb70000ceffc4000000b2ffff180013ffff55",
    "bbffab0000eeff8800000076ffff180010ffff58",
    "bbffab0000edff7e00000076ffff190014ffff52",
    "b4ffb50000cdffb1000000b1ffff470036ffff32",
    "94ffd900008cfffe712b83f5ffffd36fcaffe902",
    "46ffff1e0018f3ffffffffffe1fffffffffe6d00",
    "06f0ff6e000030d8fffffc960976d0ebbc5b0000",
    "00a5fff2240000001a2c09000000000000000000",
    "003ef7ffd12f000000000000000021aed1c20700",
    "00003deffffda3553413142a489bf4ffff8e0000",
    "0000002de4fffffffffffffffffffff068000000",
    "000000001b6c9ed0fafffffff7d26e0d00000000",
    "0000000000000000083536180100000000000000",
)
_SEP_CACHE = {}


def _sep_image(height):
    """分隔符小图（BGR），与文字条等高。字形偏下 2 像素，与游戏文字的基线对齐。"""
    if height not in _SEP_CACHE:
        glyph = np.array([[int(row[i:i + 2], 16) for i in range(0, len(row), 2)] for row in _SEP_GLYPH],
                         dtype=np.uint8)
        gh, gw = glyph.shape
        img = np.zeros((height, gw + 10), dtype=np.uint8)
        top = max(0, min(height - gh, (height - gh) // 2 + 2))
        img[top:top + gh, 5:5 + gw] = glyph[:height]
        _SEP_CACHE[height] = np.repeat(img[:, :, None], 3, axis=2)
    return _SEP_CACHE[height]


def text_right_end(strip, thr=205, pad=6, max_gap=None):
    """文字条里白字的右端（列号）。

    纯色行的字芯亮度恒高于阈值，所以不会把字估短；字右边隔开一段的亮斑（特效、亮背景）
    按「间隔超过 max_gap 就算字已结束」切掉。max_gap=0 表示不切。
    """
    cols = np.where((strip.min(axis=2) >= thr).any(axis=0))[0]
    if not len(cols):
        return 0
    if max_gap is None:
        max_gap = int(strip.shape[0] * 0.34)      # 字间空隙实测不超过 9 像素
    end = int(cols[0])
    for c in cols:
        if max_gap and c - end > max_gap:
            break
        end = int(c)
    return min(strip.shape[1], end + 1 + pad)


def pack_lines(strips, canvas=(720, 1280), max_gap=None):
    """把文字条横向拼成若干行。返回 (画布, [(roi, [这一行里各条的下标])])。"""
    h, w = canvas
    img = np.zeros((h, w, 3), dtype=np.uint8)
    lines, x, y, members, line_h = [], 0, 0, [], 0
    for i, strip in enumerate(strips):
        piece = strip[:, :max(8, text_right_end(strip, max_gap=max_gap))]
        ph, pw = piece.shape[:2]
        sep = _sep_image(ph)
        if members and x + sep.shape[1] + pw > w:
            lines.append(([0, y, x, line_h], members))
            x, y, members = 0, y + line_h + 4, []
        if y + ph > h:
            break
        if members:
            img[y:y + ph, x:x + sep.shape[1]] = sep
            x += sep.shape[1]
        pw = min(pw, w - x)
        img[y:y + ph, x:x + pw] = piece[:, :pw]
        x += pw
        line_h = ph
        members.append(i)
    if members:
        lines.append(([0, y, x, line_h], members))
    return img, lines


# ---- 读数解析 ----

ROW_RE = re.compile(r"^(?P<name>.+?)[×xX✕*](?P<count>[0-9][0-9,]{0,11})$")


def clean_text(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def parse_row(text):
    """「名字×数量」→ (名字, 数量)。整段必须恰好是这个形状，后面带任何多余字符都不认。"""
    m = ROW_RE.match(clean_text(text))
    if not m:
        return None
    digits = m.group("count").replace(",", "")
    return (m.group("name"), int(digits)) if digits else None


def split_line(text, n):
    """一行拼读的结果按分隔符拆回 n 段；段数对不上返回 None（整行不认）。"""
    pieces = clean_text(text).split(SEP)
    return pieces if len(pieces) == n else None


# ---- 会话：同一任务内跨节点共用的跟踪状态，与已播报的结果 ----

KEEP_RECORDS = 200
RECORDS = {}       # feed → 最近若干次收取的结果
_SESSIONS = {}     # (task_id, feed) → Session


class Session:
    def __init__(self, layout):
        self.tracker = FeedTracker(layout)
        self.casts = []      # 上次收取以来的点击：{"t", "tag", "confirmed", …}
        self.marks = []      # 上次收取以来的标记（不是点击，如落地补拍）：{"t", "tag", "mark": True}
        self.frames = 0


def open_session(task_id, feed, make_layout):
    """取或建会话。分桶键只用框架发的 task_id；换了任务就丢掉旧任务的状态。"""
    key = (task_id, feed)
    if key not in _SESSIONS:
        for old in [k for k in _SESSIONS if k[0] != task_id]:
            del _SESSIONS[old]
        _SESSIONS[key] = Session(make_layout())
    return _SESSIONS[key]


def peek_session(task_id, feed):
    return _SESSIONS.get((task_id, feed))


def has_pending(task_id, feed):
    """上次收取之后是否又点过技能，或有还没交出去的条目。"""
    s = peek_session(task_id, feed)
    return bool(s and (s.casts or any(not e.taken for e in s.tracker.entries)))


def add_record(feed, record):
    bucket = RECORDS.setdefault(feed, [])
    bucket.append(record)
    del bucket[:-KEEP_RECORDS]


def take_records(feed="default", clear=False):
    """取某个会话名下已播报的结果（按时间先后）。clear=True 时取走即清空。"""
    out = list(RECORDS.get(feed, ()))
    if clear:
        RECORDS.pop(feed, None)
    return out
