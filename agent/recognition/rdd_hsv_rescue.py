"""RedDotDetector 严格 HSV 救援的纯算法工具。

本模块不依赖 MaaFramework，也不负责识别副作用。它只处理：
参数校验、严格 HSV profile、切点计算、父块排序、父子 lineage 与跨档稳定选择。
运行时和离线回放共用这里，避免出现两套救援算法。

设计要点见 doc/agent/RedDotDetector/RedDotDetector_v3救援实施计划.md：
  · 切点由被卡住的父块自身颜色分布**算出**（Otsu），不枚举参数空间；
  · 亮度/饱和度两个维度都保留，三个方向（只切亮度/只切饱和/双切）先各跑一次主档，
    位置一致才选增量最小者补跑陪跑档 —— 避免三组各自成稳定分量而互判歧义；
  · 色相另成两个方向（色相上/色相下）：在"展开轴"上算一刀、两侧各试一次，
    是后手 —— 只在 S/V 没得出稳定解时补试，S/V 救得回的帧行为逐位不变；
  · 分层（max_depth>1）：一层五个切法都没得出结论时，在切出来、仍卡住的子块上
    **重算**切点再切一层；子树无解就退回上一层换下一个切法。连片的背景常不止一种，
    各差在不同维度，切掉一种之后剩下的块才在另一维上分得开；
  · 代码中不得出现来自样本的阈值常数，max_delta_* 只作安全护栏，不是工作点。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


DEFAULT_RESCUE_CONFIG: Dict[str, Any] = {
    "mode": "off",
    # 入口开关：一个前缀 entry_*，字母序下三个挨在一起且排在 max_* 之前。
    # 取值 off/shadow/active/inherit；inherit = 跟随全局 mode。
    # 缺省值即目标状态 —— 没写这三个键的 pipeline(如另一分支的 pc 包 preset)
    # 直接得到安全配置，不必逐包同步才生效：
    #   · aspect     inherit：v3 原有入口，行为与加入口之前逐位一致
    #   · confidence inherit：全语料 16 次触发 0 误救后放行(见契约 §12)
    #   · rej        shadow ：只有 1 条疑似样本，证据不足，只记录不改判
    "entry_aspect": "inherit",
    "entry_confidence": "inherit",
    "entry_confidence_rej": "shadow",
    "max_delta_s": 110,     # 护栏，非工作点
    "max_delta_v": 110,     # 护栏，非工作点
    # 分层救援的层数上限。1 = 只切一层（v3 原有行为，逐位不变）；
    # >1 时某层没得出结论，就在该层切出、仍卡住的子块上重算切点再切一层。见 _run_hsv_rescue。
    "max_depth": 1,
    "max_full_runs": 12,
    "min_stable_states": 2,
    "max_parents": 5,
    "time_budget_ms": 40,
}

_MODES = {"off", "shadow", "active"}
_ENTRY_MODES = _MODES | {"inherit"}

# 入口表：baseline 卡在哪一步 → 依次试哪些入口。
# 每项 = (配置键, 父块来源)。来源即 _detect_once 给 eligible_parents 打的"来源"标签。
#
#   A entry_aspect         长宽比闸拒绝的块（v3 原有的唯一入口）
#   B entry_confidence     过闸、打过分、但分不够的块
#   C entry_confidence_rej 打分不足帧里，同帧被长宽比拒的块（B 无解后的兜底）
#
# A 与 B/C 互斥不是约定而是结构事实：_diagnose 里 aspect_pass==0 才返回 "aspect"，
# 而没有块过闸就没有块被打分(scored=0) ⇒ stage=="aspect" 时来源 confidence 的父块必为空。
# 故新增 B/C 对既有 aspect 触发帧的父块集合、排序与预算**逐位无影响**。
ENTRY_PLANS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "aspect": (("entry_aspect", "aspect"),),
    "confidence": (("entry_confidence", "confidence"),
                   ("entry_confidence_rej", "aspect")),
}

# 已废弃的配置键：出现时忽略而非报错，避免旧 pipeline 直接 fail closed。
_OBSOLETE_KEYS = ("max_states",)

# 陪跑档与主档的间距 = 主档增量 × 该比例（下限 2）。这是"邻域"的定义，
# 是结构参数而非从样本量出的工作点：无论界面怎么变，10% 的扰动都算相邻。
_NEIGHBOR_RATIO = 0.10
_NEIGHBOR_MIN_STEP = 2

# S/V 三个切法的尝试顺序。仅影响预算消耗先后，不影响判定：
# 位置一致时取增量最小者，位置分散时一律拒绝，与顺序无关。
SV_DIRECTIONS = ("亮度", "饱和", "双切")

# 色相两个切法：**后手**，只在 S/V 没得出稳定解（一个没中 / 中了但不稳）时补试。
# 起因 GachaADV_Location1[315,130,47,42]（2026-10-28）：橙红红点(展开色相中位 1.4)
# 贴在品红卡面(中位 -11.3)上连片，肉眼色差明显，S/V 却切不开 —— 饱和 Otsu 落在
# +16~+32 的失败区，那一刀连感叹号的偏白描边一起切掉，白芯变胖、竖条与点连成一块。
# 色相刀位 -4 落在 -10~0 的成功区正中，感叹号无损。
#   色相上 = 保留展开轴上刀位及以上（对红色即切掉品红一端）
#   色相下 = 保留刀位以下（对红色即切掉橙色一端）
# 两侧都试而不是预判"红点在哪侧"：刀位由数据算出，哪侧留得住红点交给完整检测链判。
#
# 为什么不与 S/V 并列跑主档：背景与红点同色相时(暗红卡片上的 Pass_SelectPass)，
# 色相刀切不开，只削掉红点自身最纯的那几格，留下一个包着背景的虚胖框照样过闸；
# 并列时它与 S/V 的框重叠不足一半，把两帧本来救得回的样本判成方向歧义（雷电语料实测）。
# 后手则 S/V 救得回的帧一格都不碰色相，行为逐位不变。
HUE_DIRECTIONS = ("色相上", "色相下")
DIRECTIONS = SV_DIRECTIONS + HUE_DIRECTIONS

# 主档在 (si, vi) 网格里的落点。同方向的 3 档 vi 相邻(差1)，
# 不同方向 si 相隔 10 —— 保证 select_stable_winner 的 _adjacent 永不跨方向连通。
_DIRECTION_SI = {"亮度": 0, "饱和": 10, "双切": 20, "色相上": 30, "色相下": 40}

# 展开轴的折返点（OpenCV 色相刻度）。>= 此值的色相记为负数(H-180)，
# 于是绕过 0/180 的两段红色 [0,12]∪[165,180] 变成一段连续区间 [-15,12]，纯红在 0。
# 这是色相环的几何事实（把环从纯红对面剪开），不是从样本量出的工作点。
_HUE_WRAP_CV = 90


def normalize_rescue_config(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """校验配置。非法配置 fail closed 为 off，并返回原因。"""
    if raw is None:
        return dict(DEFAULT_RESCUE_CONFIG), None
    if not isinstance(raw, dict):
        cfg = dict(DEFAULT_RESCUE_CONFIG)
        return cfg, "flt_hsv_rescue 必须是 object"

    cfg = dict(DEFAULT_RESCUE_CONFIG)
    cfg.update(raw)
    for key in _OBSOLETE_KEYS:      # 旧 pipeline 残留键：忽略，不因此关闭救援
        cfg.pop(key, None)
    try:
        cfg["mode"] = str(cfg["mode"]).strip().lower()
        if cfg["mode"] not in _MODES:
            raise ValueError("mode 仅支持 off/shadow/active")

        for key in ("entry_aspect", "entry_confidence", "entry_confidence_rej"):
            cfg[key] = str(cfg[key]).strip().lower()
            if cfg[key] not in _ENTRY_MODES:
                raise ValueError(f"{key} 仅支持 off/shadow/active/inherit")

        for key in ("max_delta_s", "max_delta_v", "max_depth", "max_full_runs",
                    "min_stable_states", "max_parents", "time_budget_ms"):
            cfg[key] = int(cfg[key])

        if cfg["max_delta_s"] <= 0 or cfg["max_delta_v"] <= 0:
            raise ValueError("max_delta_s/v 必须 > 0")
        if cfg["max_full_runs"] < cfg["min_stable_states"]:
            raise ValueError("max_full_runs 必须 >= min_stable_states")
        if cfg["min_stable_states"] < 2:
            raise ValueError("min_stable_states 必须 >= 2")
        if cfg["max_parents"] < 1:
            raise ValueError("max_parents 必须 >= 1")
        if cfg["max_depth"] < 1:
            raise ValueError("max_depth 必须 >= 1")
        if cfg["time_budget_ms"] <= 0:
            raise ValueError("time_budget_ms 必须 > 0")
        if cfg["max_delta_s"] > 115 or cfg["max_delta_v"] > 135:
            raise ValueError("max_delta_s/v 超过安全上限")
        if cfg["max_full_runs"] > 32:
            raise ValueError("max_full_runs 超过安全上限 32")
        if cfg["max_parents"] > 16:
            raise ValueError("max_parents 超过安全上限 16")
        if cfg["max_depth"] > 4:
            raise ValueError("max_depth 超过安全上限 4")
        if cfg["time_budget_ms"] > 2000:
            raise ValueError("time_budget_ms 超过安全上限 2000")
    except (TypeError, ValueError) as exc:
        disabled = dict(DEFAULT_RESCUE_CONFIG)
        return disabled, str(exc)
    return cfg, None


def resolve_entry_plan(
    cfg: Dict[str, Any], stage: str,
) -> List[Tuple[str, str, str]]:
    """baseline 卡在 stage → 本次要依次试的入口 [(配置键, 父块来源, 生效mode)]。

    已过滤掉 off 的入口；返回空表即本帧不搜索。顺序即执行顺序，**不得乱序或并发**：
    「顺序敏感」是四件套里要 fail closed 的项目之一。
    """
    # mode 是硬总开关：off 时任何入口都不跑，哪怕它自己写着 active。
    # 否则"把救援整个关掉"这件事就没有单一开关可用了（契约 §6：off = 零搜索）。
    if cfg.get("mode", "off") == "off":
        return []

    plan: List[Tuple[str, str, str]] = []
    for key, source in ENTRY_PLANS.get(stage, ()):
        mode = cfg.get(key, "inherit")
        if mode == "inherit":
            mode = cfg.get("mode", "off")
        if mode in ("shadow", "active"):
            plan.append((key, source, mode))
    return plan


def strict_profile(
    ranges: Sequence[dict], delta_s: int, delta_v: int,
    hue_keep: Optional[Sequence[int]] = None,
) -> Optional[List[dict]]:
    """同步提高所有组的 S/V lower；给了 hue_keep 时再把各组 H 收进展开轴上的该区间。

    两种收紧都只缩不扩，结果必为原 profile 的子集。H 收紧后某组整段落空即删去该组，
    全部落空返回 None。什么都不收紧也返回 None —— 那不是救援。
    """
    if delta_s < 0 or delta_v < 0:
        return None
    if delta_s == 0 and delta_v == 0 and hue_keep is None:
        return None

    result: List[dict] = []
    for item in ranges:
        lower = item.get("lower") or item.get("lower_hsv")
        upper = item.get("upper") or item.get("upper_hsv")
        if not isinstance(lower, (list, tuple)) or not isinstance(upper, (list, tuple)):
            return None
        if len(lower) != 3 or len(upper) != 3:
            return None
        h_lo, h_hi = int(lower[0]), int(upper[0])
        if hue_keep is not None:
            shift = 180 if h_lo >= _HUE_WRAP_CV else 0
            h_lo = max(h_lo - shift, int(hue_keep[0])) + shift
            h_hi = min(h_hi - shift, int(hue_keep[1])) + shift
            if h_lo > h_hi:
                continue                  # 该组整段被切掉
        new_lower = [
            h_lo,
            min(255, int(lower[1]) + int(delta_s)),
            min(255, int(lower[2]) + int(delta_v)),
        ]
        new_upper = [h_hi, int(upper[1]), int(upper[2])]
        if any(new_lower[i] > new_upper[i] for i in range(3)):
            return None
        result.append({"lower": new_lower, "upper": new_upper})
    return result or None


def is_strict_mask(candidate: np.ndarray, baseline: np.ndarray) -> bool:
    """candidate 必须是 baseline 的真子集。"""
    if candidate.shape != baseline.shape:
        return False
    if np.any(candidate & ~baseline):
        return False
    return not np.array_equal(candidate, baseline)


def mask_digest(mask: np.ndarray) -> str:
    return hashlib.sha1(np.ascontiguousarray(mask).view(np.uint8)).hexdigest()


def pixel_set_key(mask: np.ndarray, x0: int, y0: int) -> str:
    """局部窗口里一块像素在 ROI 坐标系下的身份。与窗口大小、偏移无关，同一块像素同一个值。

    分层救援用它去重：不同路径切出同一块子块时，在它上面重算出的切点也相同，再想一遍是白跑。
    """
    ys, xs = np.nonzero(mask)
    coords = np.stack([ys + int(y0), xs + int(x0)]).astype(np.int32)
    return hashlib.sha1(np.ascontiguousarray(coords).tobytes()).hexdigest()


def otsu_threshold(values: np.ndarray, center_plateau: bool = False) -> Optional[int]:
    """大津法：在 0~255 上找使类间方差最大的切点，返回 t（>=t 归为"亮/饱和"的一类）。

    不需要任何预设数值，切点完全由这一堆像素自己的分布决定 —— 这是"泛用"的前提。
    已知失效条件：两类像素数悬殊会偏向大类；本来单峰时它照样给值。
    因此切点只是候选，是否采纳由后续完整检测链与跨档复现决定，不在这里下结论。

    center_plateau：两簇之间隔着空直方格时，类间方差在整段空档上**逐位相等**（累计量
    不变），argmax 只取最左端 —— 刀贴着下面那簇下。置 True 取这段平台的中点，刀落在
    空档正中。只有色相用：色相簇紧、中间常是空档；S/V 维持原行为，不动既有救援。
    """
    v = np.asarray(values, dtype=np.int64).ravel()
    if v.size < 8:
        return None
    hist = np.bincount(v, minlength=256).astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return None
    prob = hist / total
    idx = np.arange(256, dtype=np.float64)
    omega = np.cumsum(prob)
    mu = np.cumsum(prob * idx)
    mu_total = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = (mu_total * omega - mu) ** 2 / denom
    sigma_b[~np.isfinite(sigma_b)] = -1.0
    if float(sigma_b.max()) <= 0.0:
        return None                      # 完全没有可切之处（常量分布）
    if center_plateau:
        peak = np.flatnonzero(sigma_b == sigma_b.max())
        return int(peak[len(peak) // 2]) + 1
    return int(np.argmax(sigma_b)) + 1


def quantile_threshold(values: np.ndarray, q: float) -> Optional[int]:
    """分位数切点。与 Otsu 相互独立，仅用于交叉验证定点是否可信，不参与判定。"""
    v = np.asarray(values, dtype=np.int64).ravel()
    if v.size < 8:
        return None
    return int(np.percentile(v, q))


def _channel_base(ranges: Sequence[dict], index: int) -> Optional[int]:
    """取各红色组在该通道上最松的 lower（收紧以它为基准，保证是严格子集）。"""
    lows = []
    for item in ranges:
        lower = item.get("lower") or item.get("lower_hsv")
        if isinstance(lower, (list, tuple)) and len(lower) == 3:
            lows.append(int(lower[index]))
    return min(lows) if lows else None


def channel_cutpoints(parent_pixels: np.ndarray,
                      ranges: Sequence[dict],
                      config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """对一个父块算出两个通道的切点与收紧增量，附 Otsu 与分位数的分歧度。

    返回结构直接进回传字段 `救援.切点`，无需二次加工。
    """
    px = np.asarray(parent_pixels)
    if px.ndim != 2 or px.shape[0] < 8 or px.shape[1] < 3:
        return None
    s_base = _channel_base(ranges, 1)
    v_base = _channel_base(ranges, 2)
    if s_base is None or v_base is None:
        return None

    out: Dict[str, Any] = {}
    diverge = []
    for name, col, base, cap in (("饱和", 1, s_base, config["max_delta_s"]),
                                 ("亮度", 2, v_base, config["max_delta_v"])):
        cut = otsu_threshold(px[:, col])
        qcut = quantile_threshold(px[:, col], 45.0)
        delta = 0 if cut is None else max(0, min(int(cap), cut - base))
        out[name] = {"Otsu": cut, "分位": qcut, "基线": base, "增量": delta}
        if cut is not None and qcut is not None:
            diverge.append(abs(cut - qcut))
    out["分歧"] = max(diverge) if diverge else None
    hue = hue_cutpoint(px[:, 0], ranges)
    if hue is not None:
        out["色相"] = hue
    return out


def hue_axis(ranges: Sequence[dict]) -> Optional[Tuple[int, int]]:
    """配置各组 H 在展开轴（OpenCV 刻度）上的总跨度 [最小, 最大]。"""
    lows, highs = [], []
    for item in ranges:
        lower = item.get("lower") or item.get("lower_hsv")
        upper = item.get("upper") or item.get("upper_hsv")
        if not isinstance(lower, (list, tuple)) or not isinstance(upper, (list, tuple)):
            return None
        if len(lower) != 3 or len(upper) != 3:
            return None
        shift = 180 if int(lower[0]) >= _HUE_WRAP_CV else 0
        lows.append(int(lower[0]) - shift)
        highs.append(int(upper[0]) - shift)
    return (min(lows), max(highs)) if lows else None


def hue_cutpoint(pil_hue: np.ndarray,
                 ranges: Sequence[dict]) -> Optional[Dict[str, Any]]:
    """父块像素在展开轴上的 Otsu 刀位，及两侧各自保留的区间。

    像素是 Pillow 刻度(0~255)，配置是 OpenCV 刻度(0~180)：在 Pillow 刻度上展开、
    求 Otsu（分辨率更细），刀位再换回 OpenCV 整数，与配置同一坐标系。
    刀位贴着轴端(某侧为空)时不给切点 —— 那一侧什么也不剩，另一侧什么也没切。
    """
    axis = hue_axis(ranges)
    if axis is None:
        return None
    h = np.asarray(pil_hue, dtype=np.int64).ravel()
    wrap_pil = int(round(_HUE_WRAP_CV * 255.0 / 180.0))
    unwrapped = np.where(h < wrap_pil, h, h - 256)
    # otsu_threshold 只收 0~255；展开轴在 Pillow 刻度上落在 [-128,127]，平移 128 即可。
    cut = otsu_threshold(unwrapped + 128, center_plateau=True)
    if cut is None:
        return None
    t = int(round((cut - 128) * 180.0 / 255.0))
    lo, hi = axis
    if not (lo < t <= hi):
        return None
    return {"Otsu": t, "轴": [lo, hi], "上侧": [t, hi], "下侧": [lo, t - 1]}


def hue_keep_for(direction: str, cut: int,
                 axis: Sequence[int]) -> Optional[Tuple[int, int]]:
    """色相方向在刀位 cut 处保留的展开轴区间；刀位不在轴内(某侧为空/没切)时返回 None。"""
    lo, hi = int(axis[0]), int(axis[1])
    if not (lo < int(cut) <= hi):
        return None
    if direction == "色相上":
        keep = (int(cut), hi)
    elif direction == "色相下":
        keep = (lo, int(cut) - 1)
    else:
        return None
    return keep


def _neighbor_step(delta: int) -> int:
    return max(_NEIGHBOR_MIN_STEP, int(round(delta * _NEIGHBOR_RATIO)))


def direction_deltas(cutpoints: Dict[str, Any],
                     direction: str) -> Optional[Tuple[int, int]]:
    """把切点翻译成某个切法的 (delta_s, delta_v)；无效返回 None。"""
    ds = int(cutpoints.get("饱和", {}).get("增量") or 0)
    dv = int(cutpoints.get("亮度", {}).get("增量") or 0)
    pair = {"亮度": (0, dv), "饱和": (ds, 0), "双切": (ds, dv)}.get(direction)
    if pair is None or (pair[0] <= 0 and pair[1] <= 0):
        return None
    return pair


def direction_cut(cutpoints: Dict[str, Any],
                  direction: str) -> Optional[Dict[str, Any]]:
    """把切点翻译成某个切法的主档 {delta_s, delta_v, hue_cut, hue_keep}；无效返回 None。

    S/V 三个方向原样走 direction_deltas，hue_* 为 None；色相方向 delta_s/v 恒为 0。
    """
    if direction in HUE_DIRECTIONS:
        hue = cutpoints.get("色相")
        if not hue:
            return None
        keep = hue_keep_for(direction, hue["Otsu"], hue["轴"])
        if keep is None:
            return None
        return {"delta_s": 0, "delta_v": 0, "hue_cut": hue["Otsu"], "hue_keep": keep}
    pair = direction_deltas(cutpoints, direction)
    if pair is None:
        return None
    return {"delta_s": pair[0], "delta_v": pair[1], "hue_cut": None, "hue_keep": None}


def hue_neighbor_states(direction: str, cut: int,
                        axis: Sequence[int]) -> List[Dict[str, Any]]:
    """色相方向的主档 + 上下陪跑档：刀位左右各挪一步，步长规则与 S/V 同一套。

    步长按"刀位离被切那一端有多远"取比例(下限 _NEIGHBOR_MIN_STEP)，与 S/V 陪跑档
    "按增量取比例"同义：增量就是这一刀从配置边界往里收了多少。
    hue_trim = 切掉的轴长，供 select_stable_winner 选最保守档(切得最少)。
    """
    lo, hi = int(axis[0]), int(axis[1])
    trimmed = (int(cut) - lo) if direction == "色相上" else (hi - int(cut) + 1)
    step = _neighbor_step(trimmed)
    si = _DIRECTION_SI.get(direction, 0)
    out = []
    for vi, mult in ((0, -1), (1, 0), (2, 1)):
        t = int(cut) + step * mult
        keep = hue_keep_for(direction, t, axis)
        if keep is None:
            continue
        out.append({"si": si, "vi": vi, "delta_s": 0, "delta_v": 0,
                    "hue_cut": t, "hue_keep": list(keep),
                    "hue_trim": (hi - lo) - (keep[1] - keep[0])})
    return out


def neighbor_states(direction: str, delta_s: int, delta_v: int,
                    config: Dict[str, Any]) -> List[Dict[str, int]]:
    """主档 + 上下两个陪跑档，落在同一 si 上、vi 相邻，供跨档复现判定。

    陪跑档间距按主档增量的比例取，不引入绝对常数；越界或非正的档位自动剔除。
    """
    si = _DIRECTION_SI.get(direction, 0)
    step = _neighbor_step(max(delta_s, delta_v))
    cap_s, cap_v = config["max_delta_s"], config["max_delta_v"]
    out = []
    for vi, mult in ((0, -1), (1, 0), (2, 1)):
        ns = delta_s + step * mult if delta_s > 0 else 0
        nv = delta_v + step * mult if delta_v > 0 else 0
        if (delta_s > 0 and not (0 < ns <= cap_s)) or \
           (delta_v > 0 and not (0 < nv <= cap_v)):
            continue
        if ns <= 0 and nv <= 0:
            continue
        out.append({"si": si, "vi": vi, "delta_s": int(ns), "delta_v": int(nv)})
    return out


def sort_parents(parents: Sequence[dict]) -> List[dict]:
    """按"像不像红点"排先后：短边降序，短边相同按面积降序。零参数，只排序不过滤。

    真红点近似圆/菱形，短边与长边同量级；卡片上的杂红多是细横条或细竖条，短边很小。
    实测 boss-adb 5 个被拒块中正主排第 1，boss-pc 3 个中正主并列第 1。
    """
    def key(p):
        short = min(int(p.get("w", 0)), int(p.get("h", 0)))
        return (-short, -int(p.get("area", 0)), int(p.get("label", 0)))
    return sorted(parents, key=key)


def boxes_agree(boxes: Sequence[Sequence[int]], min_iou: float = 0.5) -> bool:
    """多个**切法**命中的框是否指向同一处。分散即视为方向歧义，一律拒绝。

    只用 IoU，**不用中心位移**：IoU ≥ 0.5 意味着一半以上面积重合，两个指向不同红块的
    框不可能满足（不重叠时 IoU = 0），所以 IoU 独立就完整表达了"是否同一处"。

    为什么曾经有第二道中心位移闸、又为什么要去掉：那对常数（`min_iou=0.5` /
    `max_center_shift=2.5`）原本是 `select_stable_winner` 的，用于判**相邻档位**是否
    命中同一处 —— 相邻档 ΔS 只差几个单位，切净程度几乎一样，框应当高度一致，
    2.5 像素是合理的严格判据。本函数判的是**不同切法**，而亮度与饱和是两个完全不同的
    收紧方向，切净程度天然可以差很多，同一把尺子就把"同一目标、切得不一样干净"
    误判成了"指向不同位置"。

    实测 `GachaADV_Location1[315,130,47,42]`（2026-08-29 真机）：
        亮度 Δ[0,55]  → [19,7,24,17]   ← 左边多包 6px 木纹，没切干净
        饱和 Δ[29,0]  → [25,6,18,18]
        双切 Δ[29,55] → [25,7,18,17]
    饱和与双切 IoU 0.944、中心差 0.5（几乎逐位一致）；亮度对另两者 IoU 0.718/0.750
    全部过闸，却因中心位移 3.0 > 2.5 被判方向歧义，救援 fail closed。

    `select_stable_winner` 那处**维持绝对 2.5 不动** —— 那里的场景没有这个错配。

    **两两比较，不是"都跟第一个比"**：IoU ≥ 0.5 不满足传递性。反例
    `A=(0,0,100,100) B=(0,0,50,100) C=(50,0,50,100)`：B、C 各占 A 的一半，
    IoU(A,B)=IoU(A,C)=0.5 都过闸，而 B∩C=∅ —— 只跟 first 比就会把两个不相干的
    目标判成"同一处"。这个反例要求 B、C 恰好把 A 二等分且都是 A 的子集，外接框
    实际取不到；但闸门的语义应当就是它字面上的意思，n=3 的两两比较也没有代价。
    """
    if len(boxes) <= 1:
        return True
    return all(_box_iou(left, right) >= min_iou
               for i, left in enumerate(boxes)
               for right in boxes[i + 1:])


def _same_core(left, right) -> bool:
    """两个命中的白芯有公共像素：围住的是同一个"!"，即同一个红点。"""
    return bool(left[1] & right[1])


def hits_agree(hits: Sequence[Tuple[Sequence[int], frozenset]],
               min_iou: float = 0.5) -> bool:
    """多个切法的命中是否指向同一个红点。hits = [(框, 白芯像素集合)]，两两比较。

    任一对满足其一即算同一处：
      · 框 IoU ≥ min_iou —— boxes_agree 的原判据，原来判一致的组合一个不变；
      · **两者白芯有公共像素** —— 围住的是同一个"!"。白"!"在任何收紧档下都不红，
        不同切法只改它外面那圈红的厚薄和连带的背景，"!"本身的像素不动；两个不同红点
        的"!"是不同的像素，不可能重叠。所以"同芯"比框的几何关系更直接地回答"是不是同一个"。

    起因 GachaADV_Location1（2026-10-29 真机）：亮度 Δ[0,64] → [15,7,27,22]，红点左下
    多拖一条品红卡面；饱和 Δ[30,0] → [25,7,18,16]、双切 → [25,7,17,16] 是红点本体。
    三者围住同一个"!"，亮度对饱和 IoU 0.485 只差 0.015，被判方向歧义、救援放弃。
    没用"一框包住另一框"：亮度那刀顺手削掉了红点最右一列，虚胖框反而比干净框窄 1 列。
    """
    for i, left in enumerate(hits):
        for right in hits[i + 1:]:
            if _box_iou(left[0], right[0]) >= min_iou or _same_core(left, right):
                continue
            return False
    return True


def bloated_hits(hits: Sequence[Tuple[Sequence[int], frozenset]],
                 min_iou: float = 0.5) -> set:
    """只靠"同芯"才与别的命中对上、且框更大的那些命中的下标。

    同一个"!"外面框得更大，多出来的就是连带的背景（虚胖框）。它们不参与采纳 ——
    否则增量更小的虚胖框会胜出。IoU 已过闸的组合不算，原有的采纳结果不变。
    """
    out = set()
    for i, big in enumerate(hits):
        for j, small in enumerate(hits):
            if i == j or _box_iou(big[0], small[0]) >= min_iou or not _same_core(big, small):
                continue
            if int(big[0][2]) * int(big[0][3]) > int(small[0][2]) * int(small[0][3]):
                out.add(i)
    return out


def lineage_parent(
    blob_mask: np.ndarray,
    baseline_labels: np.ndarray,
    eligible_parent_ids: Iterable[int],
) -> Optional[int]:
    """候选必须完全来自唯一的 eligible baseline 父 blob。"""
    labels = set(int(v) for v in np.unique(baseline_labels[blob_mask]))
    labels.discard(0)
    eligible = set(int(v) for v in eligible_parent_ids)
    if len(labels) != 1:
        return None
    parent = next(iter(labels))
    return parent if parent in eligible else None


def cut_happened(candidate_box: Sequence[int], parent_geo: Dict[str, Any]) -> bool:
    """候选是否真的从父块里"切"出来了 —— 外接框与父块逐位相同即判定没切开。

    救援的定义是「删低质红像素 → 切断连片 → 重跑原链」。若候选外接框与父块完全
    相同，说明一次连片都没切断，分数的变化只能来自白芯像素的增减 —— 那等价于
    契约 §4.5 明令禁止的「给救援候选补分」，只是绕道由 HSV 收紧实现。

    这道闸与四件套(严格子集/血统同源/跨档稳定/唯一性)防的不是同一类事：四件套防
    「偶然撞出一个答案」，而整片红背景是**稳定地**撞出错答案 —— 越收紧越稳定，四件套
    全部放行。实测 Daily_EnterUnion[250,72,30,32]：fill 0.79 的整片红背景，收紧后
    760→723 像素、外接框一动没动，conf 却从 0.37 跳到 0.674。

    判据是纯几何恒等比较，不含任何来自样本的常数（契约 §9）。已知软肋：外接框只缩
    1 像素也算"切开了"。要收紧只能引入比例阈值，那就是样本常数，故维持现状，
    靠语料继续暴露形态。

    ⚠️ **两个入参必须同坐标系，调用方负责换算。** 现行约定是 ROI 局部坐标：
    `_detect_once` 产出的 geometry 只存局部坐标（`rx`/`ry` 仅进 `result_box`，不进
    geometry），`_rescue_try` 把内层候选加回 `x0`/`y0` 换成同一系再送进来。真要把
    偏移塞进 geometry，这里会静默恒不相等 —— 整片红背景那类误救就再也拦不住了。
    """
    box = tuple(int(v) for v in candidate_box)
    parent = (int(parent_geo["x"]), int(parent_geo["y"]),
              int(parent_geo["w"]), int(parent_geo["h"]))
    return box != parent


def _box_iou(a: Sequence[int], b: Sequence[int]) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _center_distance(a: Sequence[int], b: Sequence[int]) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    return math.hypot((ax + aw / 2) - (bx + bw / 2),
                      (ay + ah / 2) - (by + bh / 2))


def _adjacent(a: dict, b: dict) -> bool:
    return abs(a["si"] - b["si"]) + abs(a["vi"] - b["vi"]) == 1


def select_stable_winner(
    records: Sequence[dict],
    min_stable_states: int,
    min_iou: float = 0.5,
    max_center_shift: float = 2.5,
) -> Tuple[Optional[dict], str, List[dict]]:
    """以相邻拓扑状态组成稳定图；唯一稳定分量才允许 winner。"""
    if not records:
        return None, "no_hit", []

    per_state: Dict[Tuple[int, int, int], int] = {}
    for record in records:
        key = (int(record["parent_id"]), int(record["state"]["si"]),
               int(record["state"]["vi"]))
        per_state[key] = per_state.get(key, 0) + 1
    if any(count > 1 for count in per_state.values()):
        return None, "ambiguous_split", []

    graph = [set() for _ in records]
    for i, left in enumerate(records):
        for j in range(i + 1, len(records)):
            right = records[j]
            if left["parent_id"] != right["parent_id"]:
                continue
            if not _adjacent(left["state"], right["state"]):
                continue
            if _box_iou(left["box_local"], right["box_local"]) < min_iou:
                continue
            if _center_distance(left["box_local"], right["box_local"]) > max_center_shift:
                continue
            graph[i].add(j)
            graph[j].add(i)

    components, seen = [], set()
    for start in range(len(records)):
        if start in seen:
            continue
        stack, indexes = [start], []
        seen.add(start)
        while stack:
            cur = stack.pop()
            indexes.append(cur)
            for nxt in graph[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        states = {(records[i]["state"]["si"], records[i]["state"]["vi"]) for i in indexes}
        if len(states) >= min_stable_states:
            components.append(indexes)

    support = [{
        "parent_id": records[idxs[0]]["parent_id"],
        "states": [records[i]["state"] for i in idxs],
        "boxes": [list(records[i]["box_local"]) for i in idxs],
    } for idxs in components]

    if not components:
        return None, "unstable_hit", support
    if len(components) != 1:
        return None, "ambiguous_stable_hits", support

    # 同一分量内各档必属同一切法(_adjacent 要求 si 相同)，故 S/V 增量与 hue_trim 不会混比。
    chosen = min(
        (records[i] for i in components[0]),
        key=lambda r: (r["state"]["delta_s"] + r["state"]["delta_v"]
                       + r["state"].get("hue_trim", 0),
                       r["state"]["delta_s"], r["state"]["delta_v"],
                       r.get("scan_index", 0)),
    )
    return chosen, "stable_hit", support
