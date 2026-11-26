"""RedDotDetector 换档重看的纯算法工具（不依赖 MaaFramework）。

标准档（配置里写的饱和度下限）没认出红点时，沿饱和度下限换档重跑原检测链：
往下是把洗淡的红补回来，往上是把连片的红切开，同一根轴的两个方向。
每一档的候选都要过 rdd_shape 的形状闸，并且相邻半步的一档也认出同一处，才采纳。

起因：真红点对饱和度下限并不挑 —— 2776 条语料上，同一颗红点能连续通过的档数中位是
11 档（步长 10）；亲密度增益红点的可用区间是 60~130，固定的 140 恰好在外面。
误检原先是靠 140 这个数守住的，所以放开档位的同时必须由形状来定案。
数据与验收见 priv doc/agent/RedDotDetector/RedDotDetector_补红救援.md §9。

本模块只管：配置校验、档位表、连通块标号、「是不是同一处」的判定。
驱动与副作用（重跑原链、改判、落盘）在 binarymatch.RedDotDetector._run_relevel。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


DEFAULT_RELEVEL_CONFIG: Dict[str, Any] = {
    "mode": "off",          # off / shadow（只记录不改判）/ active
    # 换档范围与步长。范围是护栏不是工作点：语料验收时扫的就是 60~220。
    "s_min": 60,
    "s_max": 220,
    "step": 10,             # 提名用的粗步长；复核用它的一半
    "time_budget_ms": 40,   # 第一档必定跑完，之后每跑一次前看墙钟
}

_MODES = {"off", "shadow", "active"}


def normalize_relevel_config(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """校验配置。非法配置 fail closed 为 off，并返回原因。"""
    if raw is None:
        return dict(DEFAULT_RELEVEL_CONFIG), None
    if not isinstance(raw, dict):
        return dict(DEFAULT_RELEVEL_CONFIG), "flt_relevel 必须是 object"

    cfg = dict(DEFAULT_RELEVEL_CONFIG)
    cfg.update(raw)
    try:
        cfg["mode"] = str(cfg["mode"]).strip().lower()
        if cfg["mode"] not in _MODES:
            raise ValueError("mode 仅支持 off/shadow/active")
        for key in ("s_min", "s_max", "step", "time_budget_ms"):
            cfg[key] = int(cfg[key])
        if not 0 <= cfg["s_min"] < cfg["s_max"] <= 255:
            raise ValueError("需要 0 <= s_min < s_max <= 255")
        if cfg["step"] < 2 or cfg["step"] % 2:
            raise ValueError("step 必须是 >= 2 的偶数（复核用半步）")
        if (cfg["s_max"] - cfg["s_min"]) // cfg["step"] > 32:
            raise ValueError("档位数超过安全上限 32")
        if not 0 < cfg["time_budget_ms"] <= 2000:
            raise ValueError("time_budget_ms 需在 (0, 2000]")
    except (TypeError, ValueError) as exc:
        return dict(DEFAULT_RELEVEL_CONFIG), str(exc)
    return cfg, None


def _bounds(rng: dict) -> Tuple[Sequence[int], Sequence[int]]:
    return (rng.get("lower") or rng.get("lower_hsv"),
            rng.get("upper") or rng.get("upper_hsv"))


def configured_floor(ranges: Sequence[dict]) -> int:
    """配置里的饱和度下限（多组取最小，即实际生效的最松一组）。"""
    return min(int(_bounds(rng)[0][1]) for rng in ranges)


def level_profile(ranges: Sequence[dict], level: int) -> List[dict]:
    """把各组红色范围的饱和度下限换成 level，色相与亮度一字不动。"""
    out = []
    for rng in ranges:
        lower, upper = _bounds(rng)
        out.append({"lower": [int(lower[0]), int(level), int(lower[2])],
                    "upper": [int(v) for v in upper]})
    return out


def coarse_levels(cfg: Dict[str, Any], floor: int) -> List[int]:
    """提名档：范围内 step 的整数倍，不含配置档（标准档已经在那一档跑过）。

    由近到远排：先试离配置档最近的，等距时先往下（洗淡的红点是立项的起因）。
    """
    step = cfg["step"]
    first = -(-cfg["s_min"] // step) * step
    levels = [lv for lv in range(first, cfg["s_max"] + 1, step) if lv != floor]
    return sorted(levels, key=lambda lv: (abs(lv - floor), lv))


def fine_neighbors(cfg: Dict[str, Any], floor: int, level: int) -> List[int]:
    """复核档：level 上下各半步，先试靠近配置档的那一侧。"""
    half = cfg["step"] // 2
    near, far = (level + half, level - half) if level < floor else (level - half, level + half)
    return [lv for lv in (near, far)
            if cfg["s_min"] <= lv <= cfg["s_max"] and lv != floor]


def label_runs(mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """4 邻接连通块标号，结果与 binarymatch._label_blobs 逐位相同，快在按行程而非按像素走。

    标号次序也相同：按每个块最靠上、其次最靠左的像素出现的先后编号。
    换档要在同一幅图上标很多遍，逐像素的广度优先撑不住。
    """
    height, width = mask.shape
    labeled = np.zeros((height, width), dtype=np.int32)
    padded = np.zeros((height, width + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    edges = np.diff(padded, axis=1)
    row_start, col_start = np.nonzero(edges == 1)
    col_end = np.nonzero(edges == -1)[1]
    count = int(row_start.size)
    if count == 0:
        return labeled, 0

    rows = row_start.tolist()
    starts = col_start.tolist()
    ends = col_end.tolist()             # 不含
    parent = list(range(count))

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    prev_lo = prev_hi = 0               # 上一行的行程下标区间
    cur_lo = 0
    for i in range(count):
        if i > 0 and rows[i] != rows[i - 1]:
            # 换行：刚结束的这一行成为「上一行」，前提是它紧挨着当前行
            if rows[i - 1] == rows[i] - 1:
                prev_lo, prev_hi = cur_lo, i
            else:
                prev_lo = prev_hi = i
            cur_lo = i
        j = prev_lo
        while j < prev_hi and ends[j] <= starts[i]:
            j += 1
        prev_lo_next = j
        while j < prev_hi and starts[j] < ends[i]:
            a, b = find(i), find(j)
            if a != b:
                # 根取下标小的那个 = 更靠上/靠左的行程，编号次序才对得上
                if a < b:
                    parent[b] = a
                else:
                    parent[a] = b
            j += 1
        # 同一行的行程按起点递增，下一个行程不会再与 prev_lo_next 之前的重叠
        prev_lo = prev_lo_next

    ids: Dict[int, int] = {}
    for i in range(count):
        root = find(i)
        label = ids.get(root)
        if label is None:
            label = ids[root] = len(ids) + 1
        labeled[rows[i], starts[i]:ends[i]] = label
    return labeled, len(ids)


def box_iou(a: Sequence[int], b: Sequence[int]) -> float:
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if not inter:
        return 0.0
    return inter / float(a[2] * a[3] + b[2] * b[3] - inter)


def same_spot(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """两档的候选是不是同一颗红点：白芯有公共像素，或框重叠过半。

    白「!」不随档位变红，不同档只改它外面那圈红；两颗不同红点的「!」不可能重叠。
    """
    return bool(a["core"] & b["core"]) or box_iou(a["box"], b["box"]) >= 0.5
