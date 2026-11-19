"""背包物品详情弹窗：判「出现动画走完」、用颜色定出文字范围、把几段文字拼成一行。只用 numpy。

弹窗出现是「上滑约 20–25 像素 + 淡入」，末尾还会越过终点 1–2 像素再回落（11-12 模拟器与 PC 逐帧实测）：点击后约 0.4 秒
画面不动，之后约 0.3 秒走完。走完的明确标志是**弹窗回到终态**：右上角关闭按钮「×」对每个物品都一样，终态时它在固定位置、
样子逐像素相同；还差一步时位置差 1–2 像素、亮度也不到顶（与终态平均差 ≥4，终态 0–2，粒子闪过时约 2）。
所以每趟第一次读完时把「×」那块记下来当参照（现场记，窗口缩放变了也对得上），之后判走完要三条同时成立：
遮罩在、「×」与参照一致、名称与数量两处与上一帧一致。还没有参照时退而要求连续三帧不变。
「画面静止多久」单独不能当信号：点击后 0.4 秒画面本来就不动，截图还会连着两次给同一帧。

读字用框架 OCR 的「只识别」（only_rec，跳过找字框那一步）：先按颜色找出文字的外接框——名称是白字，数量
「拥有…个」是浅蓝字——只把这块送去识别。名称与数量两段裁下来横向拼成一行，识别一次读出两样：本机识别模型每次调用
约 0.42 秒、与字宽几乎无关，拼起来比分开读省一次调用。设计见 priv doc/agent/bag/背包仓检_设计.md §17。
"""

from dataclasses import dataclass
import re

import numpy as np


@dataclass(frozen=True)
class DetailLayout:
    """详情弹窗的识别带（1280×720，按资源包覆盖，见 Agt_BagScan_Detail）与阈值。

    name：名称行；name_lit：名称行里不与列表、不与右侧闪烁粒子重叠的左段；quantity：数量行，往下多留一行
    （宝箱的详情多一行锁定开关，数量行下移约 33 像素）；close_icon：关闭按钮「×」终态所在的一块。
    """
    name: tuple
    name_lit: tuple
    quantity: tuple
    close_icon: tuple
    still: float = 1.0          # 两帧在文字处的平均差低于它算没变（走完后 0.00–0.01，途中 2.8–37）
    anchor_diff: float = 2.5    # 「×」与参照的平均差不超过它算回到终态（终态 0–2，差一步 ≥4）
    white_min: int = 150        # 白字：三个通道都不低于它、且彼此相差不超过 white_spread
    white_spread: int = 40
    blue_min: int = 150         # 浅蓝字：蓝通道不低于它、且比红通道高出 blue_over_red
    blue_over_red: int = 40
    line_pixels: int = 15       # 一行字至少这么多个着色像素，少于它当噪点
    pad_x: int = 6
    pad_y: int = 4

    @classmethod
    def from_attach(cls, attach: dict) -> "DetailLayout":
        values = {key: attach[key] for key in cls.__dataclass_fields__ if key in attach}
        for key in ("name", "name_lit", "quantity", "close_icon"):
            values[key] = tuple(int(v) for v in values[key])
        return cls(**values)


def _crop(image, box):
    x, y, w, h = box
    return image[y:y + h, x:x + w]


def _diff(a, b, box):
    return float(np.abs(_crop(a, box).astype(np.int16) - _crop(b, box).astype(np.int16)).mean())


def text_still(previous, image, layout: DetailLayout) -> bool:
    """名称左段与数量行都与上一帧几乎一样。"""
    return all(_diff(previous, image, box) < layout.still for box in (layout.name_lit, layout.quantity))


def anchor_of(image, layout: DetailLayout):
    """记下这一帧的「×」那块，当本趟的终态参照。"""
    return np.array(_crop(image, layout.close_icon), copy=True)


def at_anchor(image, anchor, layout: DetailLayout) -> bool:
    """「×」回到终态：与参照的平均差不超过 anchor_diff。"""
    return anchor is not None and float(
        np.abs(_crop(image, layout.close_icon).astype(np.int16) - anchor.astype(np.int16)).mean()) <= layout.anchor_diff


class FadeWatch:
    """一次点开的逐帧判定：喂进遮罩已在的帧，返回是否走完。有参照时：「×」到终态且文字与上一帧一样；
    没有参照时：文字与「×」连续三帧不变。遮罩不在的帧由调用方先挡掉并 reset。"""

    def __init__(self, layout: DetailLayout, anchor=None):
        self.layout, self.anchor = layout, anchor
        self.previous, self.run = None, 0

    def reset(self):
        self.previous, self.run = None, 0

    def feed(self, image) -> bool:
        layout = self.layout
        still = self.previous is not None and text_still(self.previous, image, layout)
        if self.anchor is None:
            still = still and _diff(self.previous, image, layout.close_icon) < layout.still
        self.run = self.run + 1 if still else 0
        self.previous = image
        if self.anchor is not None:
            return still and at_anchor(image, self.anchor, layout)
        return self.run >= 2


def _white(band, layout):
    low, high = band.min(axis=2), band.max(axis=2)
    return (low >= layout.white_min) & (high.astype(np.int16) - low <= layout.white_spread)


def _blue(band, layout):
    band = band.astype(np.int16)        # 截图是 BGR
    return (band[:, :, 0] >= layout.blue_min) & (band[:, :, 0] - band[:, :, 2] >= layout.blue_over_red)


def text_box(image, band_box, color: str, layout: DetailLayout, top_line=False):
    """band_box 里落在颜色范围内的像素的外接框（四周留白），没有返回 None。

    top_line：只取最上面那一行字。数量行在说明文字之上，说明里出现的物品名也是浅蓝字（装备材料箱「可获得低级装备材料」）。
    """
    mask = (_white if color == "white" else _blue)(_crop(image, band_box), layout)
    per_row = mask.sum(axis=1)
    rows = np.nonzero(per_row)[0]
    if not len(rows):
        return None
    if top_line:
        lines, start = [], rows[0]
        for a, b in zip(rows, rows[1:]):
            if b != a + 1:
                lines.append((start, a))
                start = b
        lines.append((start, rows[-1]))
        lines = [(a, b) for a, b in lines if per_row[a:b + 1].sum() >= layout.line_pixels]
        if not lines:
            return None
        a, b = lines[0]
        keep = np.zeros_like(mask)
        keep[a:b + 1] = mask[a:b + 1]
        mask = keep
    elif int(mask.sum()) < layout.line_pixels:
        return None
    ys, xs = np.nonzero(mask)
    x, y = band_box[0], band_box[1]
    left = max(0, x + int(xs.min()) - layout.pad_x)
    top = max(0, y + int(ys.min()) - layout.pad_y)
    right = min(image.shape[1], x + int(xs.max()) + 1 + layout.pad_x)
    bottom = min(image.shape[0], y + int(ys.max()) + 1 + layout.pad_y)
    return left, top, right - left, bottom - top


def same_text(a, b, boxes, layout: DetailLayout) -> bool:
    """读的那一帧与后来的一帧在这几块文字上几乎一样（读完关闭前的核对）。"""
    return all(_diff(a, b, box) < layout.still for box in boxes if box)


def stitch(image, boxes, gap=24):
    """把几块文字横向拼成一行（竖直居中，空隙填第一块左缘的底色），供一次识别读出几段。"""
    parts = [_crop(image, box) for box in boxes]
    height = max(part.shape[0] for part in parts)
    background = np.median(parts[0][:, :2].reshape(-1, parts[0].shape[2]), axis=0).astype(image.dtype)
    width = sum(part.shape[1] for part in parts) + gap * (len(parts) - 1)
    canvas = np.empty((height, width, image.shape[2]), image.dtype)
    canvas[:] = background
    x = 0
    for part in parts:
        y = (height - part.shape[0]) // 2
        canvas[y:y + part.shape[0], x:x + part.shape[1]] = part
        x += part.shape[1] + gap
    return np.ascontiguousarray(canvas)


def crop(image, box):
    return np.ascontiguousarray(_crop(image, box))


_SPLIT = re.compile(r"^(.*?)((?:[拥擁]有|持有)[0-9,，.．。、·]+[个個])$")
_SEPARATORS = re.compile(r"[,，.．。、·]")
# 「拥有 N 个」里的 N，以及 N 分组规整的样子：纯数字，或 1–3 位开头、之后每组正好 3 位。
# 分隔符游戏里是逗号，识别也会读成句点等（模拟器上常见，字挤的时候更是）；物品数量没有小数，一律当千分位。
_COUNT = re.compile(r"(?:[拥擁]有|持有)([0-9,，.．。、·]+)[个個]")
_GROUPED = re.compile(r"[0-9]+|[1-9][0-9]{0,2}(?:[,，.．。、·][0-9]{3})+")


def _ungroup(text: str) -> str:
    """去掉数量文字里的千分位——只在「拥有 N 个」的 N 分组规整时去；不规整的整段原样留着。

    「1,23」这种多半是漏读了一位，去掉分隔符就成了一个像模像样的错数。原样留着，扫描核心解析不了
    （它只认纯数字或规整的逗号分组，ocr_read.NUMBER），会当作数量没读清：放大重读两次、两次一致才收。
    角标（bag_corner）与出售子页读数本来就要求分组规整，这里与它们对齐。
    """
    count = _COUNT.search(text)
    if count and not _GROUPED.fullmatch(count.group(1)):
        return text
    return _SEPARATORS.sub("", text)


def split_quantity(text: str):
    """拼读的结果拆成（名称, 数量文字）；数量文字去掉千分位（见 _ungroup，分组不规整的不去）。拆不开返回 None。

    只在结尾找「拥有/持有…个」：名称里可能本来就有句点（1.5周年…），所以只对数量那一段去分隔符。
    """
    match = _SPLIT.match(re.sub(r"\s+", "", text))
    if not match or not match.group(1):
        return None
    return match.group(1), _ungroup(match.group(2))


def plain_quantity(text: str) -> str:
    """单独读到的数量文字去掉空白与千分位（见 _ungroup，分组不规整的不去）。"""
    return _ungroup(re.sub(r"\s+", "", text))
