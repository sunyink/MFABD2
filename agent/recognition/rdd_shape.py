"""RedDotDetector 的形状判据（纯算法，不依赖 MaaFramework）。

红点是「菱形 + 居中的白色感叹号」。原检测链只在颜色闸之后看白芯的竖长与偏白，
菱形本身、白芯在菱形里的位置都没参与判定。这里补上三条，全是比值、与红点大小无关。

只用来给**换档重看**（rdd_relevel）的候选定案：颜色门槛一旦允许挪动，就得由形状
来保证认出来的是红点而不是别的红色东西。标准档直接命中的候选不过这道闸 ——
轻度连片的真红点（框连着一点卡片红边）在标准档本来认得出，拿这道闸去卡会误伤。

界限的来历（priv doc/agent/RedDotDetector/RedDotDetector_补红救援.md §9）：
1539 条真命中，PC 与模拟器两端分开量，去掉被 ROI 切掉一截的和连片的之后，
两端 1333 条全部在界限内，分布没有差别（只有像素尺寸不同：PC 约 10px，模拟器约 13px）。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np


# 菱形贴合度下限：外接框内切菱形对内切圆的交并比，0.5 ÷ (π/4)。
# 含义是「至少比圆更像菱形」，是几何常数，不是从样本量出来的。
SHAPE_DIAMOND_MIN = 0.5 / (math.pi / 4.0)

# 外接框高宽比。真红点两端实测都落在 0.87~1.22。
SHAPE_ASPECT = (0.8, 1.25)

# 白芯中心相对红块中心的偏移上限，按红块宽 / 高归一。真红点两端实测都在 ±0.10 以内。
SHAPE_CENTER_MAX = 0.15


def inscribed_diamond(height: int, width: int) -> np.ndarray:
    """外接框的内切菱形（按像素中心算）。"""
    yy, xx = np.mgrid[0:height, 0:width]
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    return (np.abs(xx - cx) / (width / 2.0)
            + np.abs(yy - cy) / (height / 2.0)) <= 1.0


def shape_features(blob: np.ndarray, inner: np.ndarray) -> Dict[str, Any]:
    """量一个候选的形状。

    blob / inner：红块外接框尺寸的 bool 图 —— 红块像素、被它围住的白芯像素。
    返回的比值不取整，判定用原值；写进日志前用 public_features 取整。
    """
    height, width = blob.shape
    diamond = inscribed_diamond(height, width)
    solid = blob | inner                       # 红块 + 白芯 = 实心轮廓
    union = int((solid | diamond).sum())
    feat: Dict[str, Any] = {
        "宽": int(width), "高": int(height),
        "高宽比": height / max(width, 1),
        "菱形贴合": int((solid & diamond).sum()) / union if union else 0.0,
        "白芯横偏": None, "白芯竖偏": None,
    }
    ys, xs = np.nonzero(inner)
    if ys.size:
        feat["白芯横偏"] = (
            ((int(xs.min()) + int(xs.max()) + 1) / 2.0 - width / 2.0) / width)
        feat["白芯竖偏"] = (
            ((int(ys.min()) + int(ys.max()) + 1) / 2.0 - height / 2.0) / height)
    return feat


def public_features(feat: Dict[str, Any]) -> Dict[str, Any]:
    """取整到三位小数，供 detail / 样本台账落盘。"""
    return {key: (round(value, 3) if isinstance(value, float) else value)
            for key, value in feat.items()}


def shape_verdict(feat: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """过不过形状闸；不过时给出卡在哪一条（菱形 / 近方 / 居中）。"""
    if not feat["菱形贴合"] > SHAPE_DIAMOND_MIN:
        return False, "菱形"
    if not SHAPE_ASPECT[0] <= feat["高宽比"] <= SHAPE_ASPECT[1]:
        return False, "近方"
    dx, dy = feat["白芯横偏"], feat["白芯竖偏"]
    if dx is None or dy is None:
        return False, "居中"
    if abs(dx) > SHAPE_CENTER_MAX or abs(dy) > SHAPE_CENTER_MAX:
        return False, "居中"
    return True, None
