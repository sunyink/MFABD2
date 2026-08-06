"""只读回放 RedDotDetector_samples 的台账(samples.jsonl.log，兼容旧名 samples.jsonl)。

运行示例（从仓库根目录）：
    python agent/recognition/replay_rdd_samples.py assets/debug/RedDotDetector_samples
    python agent/recognition/replay_rdd_samples.py assets/debug/RedDotDetector_samples \
        --rescue --expect-rescue-node Pass_SelectPass

`--expect-rescue-node` 可加 ROI 限定，只对该 ROI 的样本要求救回：
    --expect-rescue-node "Pass_SelectPass@262,130,40,478"
同名节点在不同时期用过不同 ROI，历史 ROI 的样本往往救不回也不该救。
"""

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image


HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(HERE)
if AGENT_ROOT not in sys.path:
    sys.path.insert(0, AGENT_ROOT)

from recognition.binarymatch import (  # noqa: E402
    RedDotDetector,
    _FLT_AREA_DEFAULT,
    _FLT_ASPECT_DEFAULT,
    _SC_GAP_RATIO_DEFAULT,
    _SC_MIN_CONF,
)
from recognition.rdd_hsv_rescue import normalize_rescue_config  # noqa: E402
from recognition.rdd_sampler import MANIFEST_NAMES  # noqa: E402


def _load_entries(sample_dir):
    """读齐目录内所有台账。旧名在前(产生更早)，同目录两名并存时合并而非二选一。"""
    entries, used = [], []
    for name in reversed(MANIFEST_NAMES):
        path = os.path.join(sample_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        entries.extend(rows)
        used.append(f"{name}({len(rows)})")
    if not used:
        raise SystemExit(
            f"{sample_dir} 下没有台账，找过：{'、'.join(MANIFEST_NAMES)}")
    print(f"台账：{'、'.join(used)}", file=sys.stderr)
    return entries


def _load_crop(path):
    # 落盘时 rdd_sampler._save_img 已经做过 BGR→RGB，盘上就是真 RGB，
    # 按 RGB 语义直接转 HSV 即与运行侧一致 —— 别在这里"补"一次 BGR 翻转。
    return np.array(Image.open(path).convert("RGB").convert("HSV"))


def _recorded_rescue(entry):
    """兼容两代键名：`救援`(现行，与 detail 金字塔同为中文键)与 `rescue`(2026-07 的 3 条旧记录)。"""
    return entry.get("救援") or entry.get("rescue") or {}


def _rescue_impossible(outcome, area_min, asp_lo, asp_hi):
    """救援几何上不可能成功：每个被长宽比闸拒的父块，其合格后代的面积上确界都够不到下限。

    推导（`aspect = h / w`，救援是严格子集故 w'<=w、h'<=h，短边记 s=min(w,h)）：
      · 横条被拒(aspect < asp_lo)：合格要求 h'/w' >= asp_lo，即 w' <= h'/asp_lo，
        故 面积 <= w'·h' <= h'²/asp_lo <= s²/asp_lo
      · 竖条被拒(aspect > asp_hi)：合格要求 h'/w' <= asp_hi，即 h' <= asp_hi·w'，
        故 面积 <= w'·h' <= asp_hi·w'² <= asp_hi·s²
    上确界 < area_min 时，任何切法都出不来同时满足面积闸与长宽比闸的红块。
    只用 area_min / asp_lo / asp_hi 三个既有配置，不含任何来自样本的常数。

    用途：`--expect-rescue-node` 按节点名一刀切，会把这类样本一律期望救回、判成
    mismatch。有了本判据，回放器自动豁免可证不可能的样本，不必维护人工行号名单
    （名单随语料增长而失效——该形态已从 2 条涨到 10 条、跨两个独立环境）。

    注意本判据**不覆盖**"面积余量不足"的情形：父块面积仅略高于下限（实测 1.03~1.27 倍）
    时收紧必然跌破，但那取决于像素分布而非几何，无法只凭外接框断言。那类样本请用
    `--expect-rescue-node 节点名@x,y,w,h` 按 ROI 精确限定。
    """
    parents = outcome.get("eligible_parents") or []
    if not parents:
        return False
    for p in parents:
        s = min(int(p.get("w", 0)), int(p.get("h", 0)))
        cap = (s * s / asp_lo) if p.get("aspect", 0) < asp_lo else (asp_hi * s * s)
        if cap >= area_min:
            return False
    return True


def _expected_local(entry):
    """取样本的 ROI 局部坐标框 —— 与 _detect_once(rx=0, ry=0) 的产出同坐标系。

    新语料直接落了 box_local，无需换算。旧语料没有这个字段，才回退到按 mode 反推：
      · standalone：box 是全局坐标，减去 roi 原点；
      · preset：嵌套独立模式那层的 rx/ry 来自**预设节点自己的 roi**，而设计上预设
        节点只承载识别参数、不写 roi(roi 由发起调用的业务节点给)，故 rx=ry=0，box
        本身就是局部坐标。这条反推依赖的是 pipeline 侧约定，Python 保证不了——
        真给预设节点加了 roi，这里会静默错位而 result_parity 照样全绿。新语料走
        box_local 正是为了不再依赖它。
    """
    local = entry.get("box_local")
    if local:
        return list(local)
    box = entry.get("box")
    if not box:
        return None
    if entry.get("mode") == "standalone":
        roi = entry.get("roi") or [0, 0, 0, 0]
        return [box[0] - roi[0], box[1] - roi[1], box[2], box[3]]
    return list(box)


def _parse_expect_specs(specs):
    """解析 `--expect-rescue-node`：`节点名` 或 `节点名@x,y,w,h`。

    带 ROI 限定的只对该 ROI 的样本生效。加这一层是因为同名节点在不同时期用过不同
    ROI，历史 ROI 的样本往往救不回也不该救（见 §10.4 死样本），按节点名一刀切会把
    它们全判成 mismatch。返回 {节点名: None | {roi 元组}}，None 表示该节点不限 ROI。
    """
    out = {}
    for spec in specs or ():
        node, _, roi_text = spec.partition("@")
        if not roi_text:
            out[node] = None            # 不限 ROI，覆盖同节点已有的限定
            continue
        if node in out and out[node] is None:
            continue                    # 已有不限 ROI 的条目，更宽，不必再收窄
        parts = roi_text.split(",")
        if len(parts) != 4 or not all(p.strip().lstrip("-").isdigit() for p in parts):
            raise SystemExit(
                f"--expect-rescue-node 的 ROI 需要 4 个整数 x,y,w,h：{spec!r}")
        out.setdefault(node, set()).add(tuple(int(p) for p in parts))
    return out


def _expect_matches(spec_map, node, roi):
    rois = spec_map.get(node, False)
    if rois is False:
        return False
    return rois is None or tuple(roi or ()) in rois


def replay(sample_dir, rescue=False, expected_rescue_nodes=()):
    detector = RedDotDetector()
    total = parity = box_parity = rescue_stable = rescue_trigger = 0
    rescue_checks = rescue_pass = skipped_no_crop = skipped_crop_gone = 0
    rescue_exempt = 0
    checked_rescue_nodes = set()
    seen_nodes = set()          # 语料里出现过的节点名，用于区分"期望写错"与"语料不覆盖"
    mismatches = []

    entries = _load_entries(sample_dir)

    # 仅用于台账里没记 flt_hsv_rescue 的旧样本。比运行时宽松是有意的：离线没有
    # 40ms 的帧预算压力，可以让救援把该走的路走完，免得把"预算不够"误算成"救不回"。
    fallback_rescue = {
        "mode": "shadow",
        "max_delta_s": 48,
        "max_delta_v": 64,
        "max_full_runs": 24,
        "min_stable_states": 2,
        "time_budget_ms": 1000,
    }
    expect_map = _parse_expect_specs(expected_rescue_nodes)

    for index, entry in enumerate(entries, 1):
        seen_nodes.add(entry.get("node"))
        crop_name = next(
            (name for name in entry.get("files", []) if name.endswith("_roi_crop.png")),
            None,
        )
        if not crop_name:
            skipped_no_crop += 1
            mismatches.append({
                "line": index, "node": entry.get("node"),
                "error": "missing roi_crop",
            })
            continue
        crop_path = os.path.join(sample_dir, crop_name)
        if not os.path.isfile(crop_path):
            # 台账记了图、盘上却没有：用户回流包常见(只打包了部分图/图被清理过)。
            # 这是语料不全，不是算法不达标——单独计数，不进 mismatches，
            # 否则一份缺几百张图的包会把真正的 mismatch 淹掉。
            skipped_crop_gone += 1
            continue
        hsv_np = _load_crop(crop_path)
        params = entry.get("params") or {}
        hsv_ranges = (params.get("configured_hsv_ranges")
                      or params["hsv_ranges"])
        # 缺省值一律取识别器侧的同一份常量：台账当前把这些键都写全了，走不到缺省
        # 分支，但两边各写一份字面量迟早会漂——改了识别器忘了回放器是最难发现的那类。
        area_min, area_max = params.get("red_area", _FLT_AREA_DEFAULT)
        asp_lo, asp_hi = params.get("flt_aspect", _FLT_ASPECT_DEFAULT)
        min_conf = params.get("min_conf", _SC_MIN_CONF)
        gap_ratio = params.get("gap_ratio", _SC_GAP_RATIO_DEFAULT)
        outcome = detector._detect_once(
            hsv_np, hsv_ranges, area_min, area_max, asp_lo, asp_hi,
            gap_ratio, min_conf, 0, 0,
        )
        stage, _ = detector._diagnose(outcome["stat"], area_min, area_max, min_conf)
        expected_hit = entry.get("result") == "hit"
        total += 1

        rescue_result = None
        stable = False
        if rescue and not outcome["hit"] and stage == "aspect":
            rescue_trigger += 1
            raw_rescue = dict(params.get("flt_hsv_rescue") or fallback_rescue)
            raw_rescue["mode"] = "shadow"
            rescue_cfg, rescue_error = normalize_rescue_config(raw_rescue)
            if rescue_error:
                mismatches.append({
                    "line": index, "node": entry.get("node"),
                    "rescue_config_error": rescue_error,
                })
                continue
            rescue_result = detector._run_hsv_rescue(
                hsv_np=hsv_np, baseline=outcome, hsv_ranges=hsv_ranges,
                area_range=(area_min, area_max),
                aspect_range=(asp_lo, asp_hi),
                gap_ratio=gap_ratio, min_conf=min_conf, rx=0, ry=0,
                config=rescue_cfg,
            )
            stable = rescue_result.get("_decision") == "stable_hit"
            if stable:
                rescue_stable += 1

        expected_rescue = entry.get("expected_rescue")
        if (expected_rescue is None and not outcome["hit"] and stage == "aspect"
                and _expect_matches(expect_map, entry.get("node"),
                                    entry.get("roi"))):
            # 台账显式标注的 expected_rescue 一律尊重；只有由命令行推导出来的期望
            # 才走几何豁免——命令行是粗粒度猜测，人工标注不是。
            if _rescue_impossible(outcome, area_min, asp_lo, asp_hi):
                rescue_exempt += 1
            else:
                expected_rescue = True
        recorded_rescue = _recorded_rescue(entry)
        recorded_mode = recorded_rescue.get("模式") or recorded_rescue.get("mode")
        recorded_active = (recorded_mode == "active"
                           and expected_hit and not outcome["hit"])
        # 只在"旧版 active 曾经救回过"时才要求新版也救回。反向不成立：
        # 旧算法救不出，不等于新算法不该救出 —— 那正是迭代要改进的部分。
        # （2026-07-11 的 2 条 active 记录即属此类：旧网格爬失败，新直方图定点成功。）
        if (expected_rescue is None and recorded_mode == "active"
                and not outcome["hit"] and expected_hit):
            expected_rescue = True
        if expected_rescue is not None:
            rescue_checks += 1
            checked_rescue_nodes.add(entry.get("node"))
            if stable == bool(expected_rescue):
                rescue_pass += 1
            else:
                mismatches.append({
                    "line": index, "node": entry.get("node"),
                    "expected_rescue": bool(expected_rescue),
                    "actual_rescue": stable,
                    "decision": (rescue_result or {}).get("_decision"),
                    "stop_reason": (rescue_result or {}).get("停因"),
                })
            if recorded_active and stable:
                winner = rescue_result.get("_winner")
                actual_box = (list(winner["candidate"]["box_local"])
                              if winner is not None else None)
                expected_box = _expected_local(entry)
                if actual_box != expected_box:
                    mismatches.append({
                        "line": index, "node": entry.get("node"),
                        "expected_active_box": expected_box,
                        "actual_active_box": actual_box,
                    })
                actual_profile = (winner.get("profile")
                                  if winner is not None else None)
                expected_profile = params.get("effective_hsv_ranges")
                if expected_profile is not None and actual_profile != expected_profile:
                    mismatches.append({
                        "line": index, "node": entry.get("node"),
                        "expected_active_profile": expected_profile,
                        "actual_active_profile": actual_profile,
                    })

        expected_baseline_hit = False if recorded_active else expected_hit
        if outcome["hit"] == expected_baseline_hit:
            parity += 1
        else:
            mismatches.append({
                "line": index, "node": entry.get("node"),
                "expected_baseline": "hit" if expected_baseline_hit else "miss",
                "actual": "hit" if outcome["hit"] else stage,
            })
            continue

        if expected_hit and not recorded_active:
            actual_box = list(outcome["candidates"][0]["box_local"])
            expected_box = _expected_local(entry)
            if actual_box == expected_box:
                box_parity += 1
            else:
                mismatches.append({
                    "line": index, "node": entry.get("node"),
                    "expected_box": expected_box, "actual_box": actual_box,
                })

    if total == 0:
        mismatches.append({"error": "no replayable samples"})
    not_applicable = []
    for node in set(expect_map) - checked_rescue_nodes:
        # 该节点一条都没检查到，分两种情况，后果完全不同：
        #   · 节点名压根没在语料里出现 → 期望写错了，是真错误
        #   · 节点出现过但没有匹配的 aspect 帧（如 ROI 限定到了本语料不含的配置，
        #     或该节点的样本全被几何豁免）→ 这份语料不覆盖该场景，不是失败
        # 不区分的话，"对每份回流包跑同一条回归命令"永远不可能干净收敛。
        report = {"node": node,
                  "error": "no aspect-stage rescue sample was checked",
                  "exempted": rescue_exempt}
        if node in seen_nodes:
            report["error"] = ("not covered by this corpus "
                               "(node present, no matching aspect frame)")
            not_applicable.append(report)
        else:
            mismatches.append(report)

    print(json.dumps({
        "total": total,
        "result_parity": parity,
        "hit_box_parity": box_parity,
        "skipped_no_crop": skipped_no_crop,
        "skipped_crop_gone": skipped_crop_gone,
        "rescue_trigger": rescue_trigger,
        "rescue_stable": rescue_stable,
        "rescue_checks": rescue_checks,
        "rescue_pass": rescue_pass,
        "rescue_exempt": rescue_exempt,
        "not_applicable": not_applicable,
        "mismatches": mismatches,
    }, ensure_ascii=False, indent=2))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sample_dir")
    parser.add_argument("--rescue", action="store_true")
    parser.add_argument("--expect-rescue-node", action="append", default=[])
    args = parser.parse_args()
    raise SystemExit(replay(
        os.path.abspath(args.sample_dir), args.rescue, args.expect_rescue_node))
