"""Current-row cartridge reading shared by all resource packs.

Type text, line geometry and the numeric suffix are separate evidence. All
fallback OCR stays within the caller's current-row bounds; no monthly value is
used to fill a current cartridge.
"""

from copy import deepcopy
import math
import re
import unicodedata

from . import mfaalog
from .arbitrage_number_geometry import analyze_number_region, build_number_crops


RESCUE_NODE = "Arbitrage_Sell_Cart_RescueNum"
DEFAULT_CONFIG = {
    "min_score": 0.6,
    "initial_min_score": 0.85,
    "number_crop_x_padding_fracs": [0.5, 1.0],
    "number_crop_bottom_padding": 2,
    "number_band_height_ratio": [0.5, 1.5],
    "type_height_range": [10, 25],
    "type_height": 17,
    "number_height": 16,
    "number_left_frac": 0.6,
    "number_right_pad": 10,
    "scan_step": 3,
    "max_scan_windows": 24,
    "number_ranges": {"story": [1, 19], "character": [1, 7], "event": [1, 7], "manager": []},
    "type_labels": {"story": "剧情游戏卡", "character": "角色游戏卡",
                    "event": "活动游戏卡", "manager": "店长游戏卡"},
    "type_patterns": {
        "story": [r"[剧劇]情[游遊][戏戲]卡(?:[带帶])?", r"故事[游遊][戏戲]卡(?:[带帶])?"],
        "character": [r"角色[游遊][戏戲]卡(?:[带帶])?"],
        "event": [r"活[动動][游遊][戏戲]卡(?:[带帶])?"],
        "manager": [r"店[长長][游遊][戏戲]卡(?:[带帶])?"],
    },
}
_TYPE_ALIASES = {"story": ("剧情", "劇情", "故事"), "character": ("角色",),
                 "event": ("活动", "活動"), "manager": ("店长", "店長")}


def load_config(context):
    cfg = deepcopy(DEFAULT_CONFIG)
    try:
        attach = getattr(context.get_node_object(RESCUE_NODE), "attach", None) or {}
    except Exception as exc:
        mfaalog.warning(f"[Arbitrage] 卡带救援配置不可读，使用默认值: {exc}")
        return cfg
    for key, default in DEFAULT_CONFIG.items():
        if key not in attach:
            continue
        value = attach[key]
        valid = False
        if isinstance(default, dict):
            if isinstance(value, dict):
                for kind, original in default.items():
                    candidate = value.get(kind, original)
                    if key == "type_labels":
                        ok = isinstance(candidate, str) and bool(candidate.strip())
                    elif key == "type_patterns":
                        ok = isinstance(candidate, list) and bool(candidate) and all(
                            isinstance(pattern, str) and pattern.strip() for pattern in candidate)
                        if ok:
                            try:
                                for pattern in candidate:
                                    re.compile(pattern)
                            except re.error:
                                ok = False
                    else:
                        ok = isinstance(candidate, list) and (not candidate or
                            len(candidate) == 2 and all(type(v) is int for v in candidate)
                            and 1 <= candidate[0] <= candidate[1] <= 99)
                    if ok:
                        cfg[key][kind] = deepcopy(candidate)
                    else:
                        mfaalog.warning(f"[Arbitrage] 卡带救援参数 {key}.{kind} 无效，使用默认值")
                continue
        elif isinstance(default, list):
            valid = isinstance(value, list) and bool(value) and all(
                type(v) in (int, float) and math.isfinite(v) for v in value)
            if key == "number_crop_x_padding_fracs":
                valid = valid and len(value) == 2 and 0 <= value[0] < value[1] <= 2
            elif key == "number_band_height_ratio":
                valid = valid and len(value) == 2 and 0 < value[0] < value[1] <= 4
            else:
                valid = valid and len(value) == 2 and 1 <= value[0] <= value[1] <= 50
        elif type(default) is int:
            minimum = 0 if key == "number_crop_bottom_padding" else 1
            valid = type(value) is int and minimum <= value <= 64
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= 2
            if key in ("min_score", "initial_min_score", "number_left_frac"):
                valid = valid and value < 1
        if valid:
            cfg[key] = deepcopy(value)
        else:
            mfaalog.warning(f"[Arbitrage] 卡带救援参数 {key} 无效，使用默认值")
    return cfg


def _clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def classify_type(text):
    """Return a family and whether its distinctive prefix is complete."""
    text = _clean(text)
    matches = {kind for kind, aliases in _TYPE_ALIASES.items() if any(text.startswith(a) for a in aliases)}
    if len(matches) == 1:
        kind = next(iter(matches))
        if any(alias in text[2:] for other, aliases in _TYPE_ALIASES.items() if other != kind for alias in aliases):
            return "", False
        return kind, True
    # Only distinctive leading characters help; the shared 游戏卡 suffix is not evidence.
    prefix = re.split(r"[游遊避煎][戏戲些戴]?|[0-9]", text, maxsplit=1)[0]
    if not 1 <= len(prefix) <= 2:
        return "", False
    candidates = {kind for kind, aliases in _TYPE_ALIASES.items()
                  if any(len(prefix) == 1 and prefix in alias for alias in aliases)}
    return (next(iter(candidates)), False) if len(candidates) == 1 else ("", False)


def valid_number(number, kind, cfg):
    if not re.fullmatch(r"[1-9][0-9]?", number or ""):
        return False
    ranges = cfg["number_ranges"]
    choices = [ranges.get(kind, [])] if kind else list(ranges.values())
    return any(pair and pair[0] <= int(number) <= pair[1] for pair in choices)


def cartridge_identity(raw, cfg=None):
    """Resolve a complete display label to its language-independent type and number."""
    cfg = cfg if cfg is not None else DEFAULT_CONFIG
    text = _clean(raw)
    identities = set()
    for kind, patterns in cfg["type_patterns"].items():
        for pattern in patterns:
            match = re.fullmatch(r"(?:" + pattern + r")(?P<cartridge_number>[0-9]+)", text)
            if match and valid_number(match["cartridge_number"], kind, cfg):
                identities.add((kind, match["cartridge_number"]))
    return next(iter(identities)) if len(identities) == 1 else None


def cartridge_expected(raw, cfg=None):
    """Return all supported display rules; never dispatch an unknown or invalid cartridge."""
    cfg = cfg if cfg is not None else DEFAULT_CONFIG
    identity = cartridge_identity(raw, cfg)
    if identity is None:
        raise ValueError(f"卡带类型或编号未确认: {raw}")
    kind, number = identity
    return list(dict.fromkeys(
        r"(?:" + pattern + r")(?<!\d)" + number + r"(?!\d)"
        for pattern in cfg["type_patterns"][kind]
    ))


def _clip(roi, bounds):
    left, top, right, bottom = bounds
    x1, y1 = max(roi[0], left), max(roi[1], top)
    x2, y2 = min(roi[0] + roi[2], right), min(roi[1] + roi[3], bottom)
    # Round inward: never cross the caller's boundary because of truncation.
    x1, y1, x2, y2 = math.ceil(x1), math.ceil(y1), math.floor(x2), math.floor(y2)
    return [x1, y1, x2 - x1, y2 - y1] if x2 > x1 and y2 > y1 else None


class _Reader:
    def __init__(self, context, image, bounds, cfg):
        self.context, self.image, self.bounds, self.cfg = context, image, bounds, cfg
        self.cache = {}
        self.attempts = []
        self.error = None

    def read(self, roi, only_rec=True):
        clipped = _clip(roi, self.bounds)
        if getattr(getattr(self.context, "tasker", None), "stopping", False):
            self.error = "stopped"
            return []
        if not clipped:
            self.error = "invalid_crop"
            return []
        key = (*clipped, only_rec)
        if key in self.cache:
            return self.cache[key]
        try:
            reco = self.context.run_recognition(RESCUE_NODE, self.image, pipeline_override={RESCUE_NODE: {
                "recognition": "OCR", "roi": clipped, "only_rec": only_rec}})
            if reco is None:
                raise RuntimeError("编号OCR未执行")
            # Low-score legal rivals must remain visible to the consensus check.
            results = getattr(reco, "all_results", None)
            if results is None:
                results = getattr(reco, "filtered_results", None) or []
        except Exception as exc:
            mfaalog.warning(f"[Arbitrage] 卡带局部OCR异常: {exc}")
            self.error = "ocr_error"
            results = []
        out = []
        for result in results:
            box = getattr(result, "box", clipped)
            if hasattr(box, "x"):
                box = [box.x, box.y, box.w, box.h]
            x, y, w, h = box
            score = float(getattr(result, "score", 0))
            score = score if math.isfinite(score) and 0 <= score <= 1 else 0.0
            out.append({"text": _clean(getattr(result, "text", "")), "score": score,
                        "x": x, "y": y, "w": w, "h": h, "cx": x + w / 2, "cy": y + h / 2})
        self.cache[key] = out
        self.attempts.append({"roi": clipped, "only_rec": only_rec,
                              "clipped": list(roi) != clipped,
                              "error": self.error,
                              "reads": [{"text": d["text"], "score": round(d["score"], 4)} for d in out]})
        return out


def _number_reads(reader, specs, kind):
    evidence = {}
    for spec in specs:
        spec["stage"] = "complete_number"
        spec["reads"] = []
        if not spec["admitted"]:
            continue
        roi = spec["roi"]
        for det in reader.read(roi):
            number = det["text"]
            legal = valid_number(number, kind, reader.cfg)
            spec["reads"].append({"raw_text": number, "number": number if legal else None,
                                  "score": det["score"],
                                  "supporting": legal and det["score"] >= reader.cfg["initial_min_score"]})
            if not legal:
                continue
            evidence.setdefault(number, []).append({"roi": list(roi), "score": det["score"]})
        if reader.error or getattr(getattr(reader.context, "tasker", None), "stopping", False):
            spec["error"] = reader.error or "stopped"
            reader.error = spec["error"]
            break
    return evidence


def _choose_number(evidence, support_floor, digit_count):
    if len(evidence) > 1:
        return "", 0.0, "complete_crop_conflict"
    if not evidence:
        return "", 0.0, "no_legal_number"
    number, observations = next(iter(evidence.items()))
    if len(number) != digit_count:
        return "", 0.0, "digit_shape_disagreement"
    supports = {}
    for observation in observations:
        if observation["score"] >= support_floor:
            y = observation["roi"][1]
            supports[y] = max(supports.get(y, 0), observation["score"])
    if len(supports) < 2:
        return "", 0.0, "insufficient_positions"
    return number, min(supports.values()), "complete_crop_consensus"


def rescue_tail_num(context, screenshot, type_dets, cfg, bounds, number_dets=(), kind=""):
    """Compatibility entry; numeric rescue shares the same geometry and decision path."""
    _, score, _, meta = read_current([*type_dets, *number_dets], context, screenshot, cfg, bounds, "尾号复核")
    if kind and meta.get("type") != kind:
        return "", 0.0
    return meta.get("number", ""), score


def _sweep(bounds, height, cfg):
    left, top, right, bottom = bounds
    starts = range(math.ceil(top), math.floor(bottom), cfg["scan_step"])
    return [[left, y, right - left, height] for y in list(starts)[:cfg["max_scan_windows"]]]


def _normal_type(det, cfg):
    low, high = cfg["type_height_range"]
    return low <= det["h"] <= high


def read_current(dets, context, screenshot, cfg, bounds, label):
    """Return display text, score, reason and independent type/number/geometry evidence."""
    cfg = {**deepcopy(DEFAULT_CONFIG), **cfg}
    meta = {"type_basis": "unknown", "number_basis": "unknown", "geometry_basis": "unknown"}
    if bounds is None:
        return "", 0.0, "unreadable_region", meta
    typed = [d for d in dets if re.search(r'[^\W\d_]', d["text"])]
    region = analyze_number_region(screenshot, bounds, typed, cfg)
    meta.update(region.detail)
    if region.detail["reason"] in ("invalid_image", "invalid_bounds", "invalid_strip", "uniform_or_empty"):
        meta["decision_reason"] = region.detail["reason"]
        return "", 0.0, "unconfirmed_current_number", meta
    reader = _Reader(context, screenshot, region.detail["bounds"], cfg)
    if getattr(getattr(context, "tasker", None), "stopping", False):
        meta["decision_reason"] = "stopped"
        return "", 0.0, "unconfirmed_current_number", meta
    numbers = [d for d in dets if re.fullmatch(r"[0-9]+", _clean(d["text"]))]
    known = [(d, *classify_type(d["text"])) for d in typed]
    families = {kind for _, kind, _ in known if kind}
    kind = next(iter(families)) if len(families) == 1 else ""
    complete = bool(kind) and any(k == kind and full and d["score"] >= cfg["min_score"] for d, k, full in known)
    type_score = min((d["score"] for d, k, _ in known if k == kind), default=0.0)
    normal = [d for d in typed if _normal_type(d, cfg)]
    geometry = bool(normal) and len(normal) == len(typed)
    if complete:
        meta["type_basis"] = "known_prefix"
    # A large box may be re-detected into two lines. Do this once, not recursively.
    if typed and not geometry:
        left, top = min(d["x"] for d in typed), min(d["y"] for d in typed)
        right = max(d["x"] + d["w"] for d in typed)
        bottom = max(d["y"] + d["h"] for d in typed)
        redetected = reader.read([left, top, right - left, bottom - top], False)
        recovered = [(d, *classify_type(d["text"])) for d in redetected]
        sane = [d for d, k, full in recovered if k and full and _normal_type(d, cfg)
                and d["score"] >= cfg["min_score"] and (not kind or k == kind)]
        if sane:
            typed, normal, geometry = sane, sane, True
            kind, complete = classify_type(sane[0]["text"])
            type_score = min(d["score"] for d in sane)
            numbers = [d for d in redetected if re.fullmatch(r"[0-9]+", d["text"])] or numbers
            meta["type_basis"] = "local_redetect"
    if not complete:
        candidates = {}
        for roi in _sweep(reader.bounds, cfg["type_height"], cfg):
            for det in reader.read(roi):
                family, full = classify_type(det["text"])
                if not family or not full or det["score"] < cfg["min_score"]:
                    continue
                candidates.setdefault(family, []).append(det)
        if len(candidates) == 1 and (not kind or kind in candidates):
            kind = next(iter(candidates))
            hits = candidates[kind]
            if len({d["y"] for d in hits}) >= 2:
                complete = True
                typed = hits
                type_score = min(d["score"] for d in hits)
                meta["type_basis"] = "type_sweep"
        if not complete:
            meta["attempts"] = reader.attempts
            mfaalog.warning(f"[Arbitrage] 当前卡带类型未确认: {label}，不按数字猜类型")
            return "", 0.0, "unconfirmed_current_type", meta
    body = cfg["type_labels"][kind]
    meta["type"] = kind
    region = analyze_number_region(screenshot, reader.bounds, typed, cfg)
    meta.update(region.detail)
    meta["geometry_basis"] = "pixel_bands"
    meta["attempts"] = reader.attempts
    if reader.error:
        meta["decision_reason"] = reader.error
        return body, type_score, "unconfirmed_current_number", meta
    # Inline numbers may belong to an otherwise correctly detected type line.
    inline = [re.search(r"([0-9]+)$", _clean(d["text"])) for d in typed]
    originals = [m.group(1) for m in inline if m] + [d["text"] for d in numbers]
    originals = list(dict.fromkeys(originals))
    original = originals[0] if len(originals) == 1 else ""
    number_score = min((d["score"] for d in numbers), default=type_score)
    if region.detail["status"] == "inline_candidate":
        band = region.detail["type_band"]
        same_line = all(band[0] <= d["cy"] < band[1] for d in numbers)
        if (geometry and same_line and len(originals) == 1 and valid_number(original, kind, cfg)
                and number_score >= cfg["initial_min_score"]):
            meta.update(number=original, number_basis="initial_complete", decision_reason="inline_complete")
            return body + original, min(type_score, number_score), "current_row", meta
        meta["decision_reason"] = "inline_number_conflict" if len(originals) > 1 else "inline_number_unconfirmed"
        return body, type_score, "unconfirmed_current_number", meta
    if region.detail["status"] != "wrapped_bounded":
        meta["decision_reason"] = region.detail["reason"]
        return body, type_score, "unconfirmed_current_number", meta
    specs = build_number_crops(region, cfg)
    meta["crops"] = specs
    if len({s["roi"][1] for s in specs if s["admitted"]}) < 2:
        meta["decision_reason"] = "insufficient_positions"
        return body, type_score, "unconfirmed_current_number", meta
    evidence = _number_reads(reader, specs, kind)
    number, score, reason = _choose_number(evidence, cfg["initial_min_score"], len(region.detail["blocks"]))
    if reader.error:
        number, score, reason = "", 0.0, reader.error
    meta["attempts"] = reader.attempts
    meta["number_candidates"] = sorted(evidence)
    meta["number_scores"] = {token: round(max(d["score"] for d in reads), 4) for token, reads in evidence.items()}
    meta["decision_reason"] = reason
    if number:
        meta.update(number=number, number_basis="number_geometry")
        mfaalog.info(f"[Arbitrage] 当前编号救援: {label} {original or '缺号'}→{number}")
        return body + number, min(type_score, score), "current_number_rechecked", meta
    meta["number_basis"] = reason
    mfaalog.warning(f"[Arbitrage] 当前编号未确认: {label}，原因={reason}，候选={sorted(evidence)}")
    return body, type_score, "unconfirmed_current_number", meta
