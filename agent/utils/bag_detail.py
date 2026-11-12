"""背包物品详情弹窗：判「淡入走完」、用颜色定出文字范围、把几段文字拼成一行。只用 numpy。

弹窗是原地淡入（11-12 模拟器与 PC 实测）：点击后约 0.4 秒遮罩一帧压暗，弹窗不移动、不缩放，透明度约 0.3 秒涨到 1。
淡入途中名称白字最亮只到 240 出头，走完才有接近纯白（≥250）的像素；走完后相邻两帧几乎一样（平均差 0.00–0.01，
淡入途中 2.8–37）。截图偶尔连续两次返回同一帧，所以「和上一帧一样」不能单独当走完的信号，要和白字的亮度一起看。

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

    name：名称行；name_lit：名称行里不与列表重叠的左段（判淡入走完）；quantity：数量行，往下多留一行
    （宝箱的详情多一行锁定开关，数量行下移约 33 像素）。
    """
    name: tuple
    name_lit: tuple
    quantity: tuple
    lit: int = 250              # 名称白字走完时有这么亮的像素（三个通道都不低于它）
    lit_count: int = 3          # 至少这么多个
    steady: float = 1.0         # 名称左段与上一帧的平均差低于它算静止
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
        for key in ("name", "name_lit", "quantity"):
            values[key] = tuple(int(v) for v in values[key])
        return cls(**values)


def _crop(image, box):
    x, y, w, h = box
    return image[y:y + h, x:x + w]


def bright(image, layout: DetailLayout) -> bool:
    """名称左段里有接近纯白的字：弹窗淡入已到（或几乎到）终点。"""
    band = _crop(image, layout.name_lit)
    return int((band.min(axis=2) >= layout.lit).sum()) >= layout.lit_count


def steady(previous, image, layout: DetailLayout) -> bool:
    """名称左段与上一帧几乎一样。"""
    a = _crop(previous, layout.name_lit).astype(np.int16)
    b = _crop(image, layout.name_lit).astype(np.int16)
    return float(np.abs(a - b).mean()) < layout.steady


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
    x, y, w, h = band_box
    left = max(0, x + int(xs.min()) - layout.pad_x)
    top = max(0, y + int(ys.min()) - layout.pad_y)
    right = min(image.shape[1], x + int(xs.max()) + 1 + layout.pad_x)
    bottom = min(image.shape[0], y + int(ys.max()) + 1 + layout.pad_y)
    return left, top, right - left, bottom - top


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


def split_quantity(text: str):
    """拼读的结果拆成（名称, 数量文字）；数量文字去掉千分位（句点、逗号都可能读到）。拆不开返回 None。

    只在结尾找「拥有/持有…个」：名称里可能本来就有句点（1.5周年…），所以只对数量那一段去分隔符。
    """
    match = _SPLIT.match(re.sub(r"\s+", "", text))
    if not match or not match.group(1):
        return None
    return match.group(1), _SEPARATORS.sub("", match.group(2))


def plain_quantity(text: str) -> str:
    """单独读到的数量文字去掉空白与千分位。"""
    return _SEPARATORS.sub("", re.sub(r"\s+", "", text))
