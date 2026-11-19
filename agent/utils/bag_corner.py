"""背包格子右下角的数量字：按白字定出裁块、解析读数。只用 numpy。

列表上每格右下角压着这一格的数量（白字，千分位带逗号）。仓检对「图标认定、今天前一次已经点开读过数」的格子，
先读角标，与前一次一样才不点开（用户 11-16，设计见 priv doc/agent/bag/背包仓检_设计.md §23）。

裁块不用整片检测（检测模型对这种小字不可靠）：格子下沿一条带里找白字，从最右边的白色列往左走，
字与字（含逗号）之间的空隙不超过 gap 就接着走；走到头之后再往左看 guard 宽，还有白字就是边界不清
（最左边的「1」、逗号离得远，11-16 模拟器截图上「1,200」只裁到「200」）——宁可点开，不读半截数。
解析只认规范千分位：四位以上必须带逗号，不认「1200」（图标白边被读成一个「1」时会是这样）。
"""

from dataclasses import dataclass
import re
import unicodedata

import numpy as np


# 千分位：游戏里是逗号，OCR 也会读成句点等（与详情弹窗 bag_detail._SEPARATORS 同一组）；分组必须规整。
CORNER_PATTERN = re.compile(r"[1-9][0-9]{0,2}(?:[,，.．。、·][0-9]{3})*")
_SEPARATORS = re.compile(r"[,，.．。、·]")


@dataclass(frozen=True)
class CornerLayout:
    """角标的找字参数（按资源包覆盖，见 Agt_BagScan_Grid.attach.corner）。

    band：条带从格子高度的这个比例起往下（与判空格的数量字条带同一块）；white_min / white_spread：白字的亮度下限、
    三通道相差上限；gap：同一串字里列与列的空隙上限；guard：左沿外再看这么宽；edge：字串右端离条带右沿不超过它
    （数量字右对齐，离得远的白色是图标）；pad：裁块四周留白。
    """
    band: float = 0.74
    white_min: int = 150
    white_spread: int = 40
    gap: int = 8
    guard: int = 16
    edge: int = 16
    pad: int = 3

    @classmethod
    def from_attach(cls, attach) -> "CornerLayout":
        attach = attach or {}
        return cls(**{key: attach[key] for key in cls.__dataclass_fields__ if key in attach})


def corner_box(image, cell_box, layout: CornerLayout):
    """格子右下角数量字的裁块 [x, y, w, h]；找不到字或边界不清返回 None。image 为 BGR 截图。"""
    x, y, w, h = (int(v) for v in cell_box)
    top, bottom, left, right = y + int(h * layout.band), y + h - 2, x + 4, x + w - 2
    if bottom - top < 4 or right - left < 8:
        return None
    band = np.asarray(image)[top:bottom, left:right].astype(np.int16)
    low, high = band.min(axis=2), band.max(axis=2)
    white = (low >= layout.white_min) & (high - low <= layout.white_spread)
    columns = np.nonzero(white.sum(axis=0))[0]
    if not len(columns):
        return None
    end = int(columns[-1])
    if end < band.shape[1] - 1 - layout.edge:
        return None
    start = end
    for column in columns[::-1][1:]:
        if start - int(column) > layout.gap:
            break
        start = int(column)
    if any(start - layout.gap - layout.guard <= int(column) < start - layout.gap for column in columns):
        return None
    rows = np.nonzero(white[:, start:end + 1].sum(axis=1))[0]
    x0 = max(left + start - layout.pad, x)
    x1 = min(left + end + 1 + layout.pad, x + w)
    y0 = max(top + int(rows[0]) - layout.pad, y)
    y1 = min(top + int(rows[-1]) + 1 + layout.pad, y + h)
    return [x0, y0, x1 - x0, y1 - y0]


def parse_corner(texts):
    """识别结果 → 数量；只认一个分组规整的整数（去空白；四位以上必须有千分位），否则 None。

    「1.1M」「1:390」「05」「1200」都不认：缩写、读错的分隔、截掉了开头、图标白边被读成「1」。
    """
    values = set()
    for text in texts:
        value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))
        if CORNER_PATTERN.fullmatch(value):
            values.add(int(_SEPARATORS.sub("", value)))
    return next(iter(values)) if len(values) == 1 else None
