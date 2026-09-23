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


RESCUE_NODE = "Arbitrage_Sell_Cart_RescueNum"
DEFAULT_CONFIG = {
    "min_score": 0.6,
    "initial_min_score": 0.85,
    "score_drop": 0.25,
    "narrow_frac": 0.5,
    "pad_frac": 0.1,
    "h_frac": 1.2,
    "y_shifts": [-0.2, 0.0, 0.2],
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
            if key == "y_shifts":
                valid = valid and len(value) <= 12 and all(-1 <= v <= 2 for v in value)
            else:
                valid = valid and len(value) == 2 and 1 <= value[0] <= value[1] <= 50
        elif type(default) is int:
            valid = type(value) is int and 1 <= value <= 64
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= 2
            if key in ("min_score", "initial_min_score", "score_drop", "number_left_frac"):
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


def rescue_rois(type_dets, cfg):
    left = min(d["x"] for d in type_dets)
    right = max(d["x"] + d["w"] for d in type_dets)
    bottom = max(d["y"] + d["h"] for d in type_dets)
    heights = sorted(d["h"] for d in type_dets)
    height = heights[len(heights) // 2]
    width = max(1, right - left)
    pad = max(1, round(cfg["pad_frac"] * width))
    narrow = max(1, round(cfg["narrow_frac"] * width))
    crop_height = max(1, round(cfg["h_frac"] * height))
    result = []
    for shift in cfg["y_shifts"]:
        top = round(bottom + shift * height)
        result.extend(([left - pad, top, width + 2 * pad, crop_height],
                       [right - narrow, top, narrow + pad, crop_height]))
    return result


class _Reader:
    def __init__(self, context, image, bounds, cfg):
        self.context, self.image, self.bounds, self.cfg = context, image, bounds, cfg
        self.cache = {}
        self.attempts = []

    def read(self, roi, only_rec=True):
        clipped = _clip(roi, self.bounds)
        if not clipped or getattr(getattr(self.context, "tasker", None), "stopping", False):
            return []
        key = (*clipped, only_rec)
        if key in self.cache:
            return self.cache[key]
        try:
            reco = self.context.run_recognition(RESCUE_NODE, self.image, pipeline_override={RESCUE_NODE: {
                "recognition": "OCR", "roi": clipped, "only_rec": only_rec}})
            results = getattr(reco, "filtered_results", None) or getattr(reco, "all_results", None) or []
        except Exception as exc:
            mfaalog.warning(f"[Arbitrage] 卡带局部OCR异常: {exc}")
            results = []
        out = []
        for result in results:
            box = getattr(result, "box", clipped)
            if hasattr(box, "x"):
                box = [box.x, box.y, box.w, box.h]
            x, y, w, h = box
            out.append({"text": _clean(getattr(result, "text", "")), "score": getattr(result, "score", 0),
                        "x": x, "y": y, "w": w, "h": h, "cx": x + w / 2, "cy": y + h / 2})
        self.cache[key] = out
        self.attempts.append({"roi": clipped, "only_rec": only_rec,
                              "clipped": list(roi) != clipped,
                              "reads": [{"text": d["text"], "score": round(d["score"], 4)} for d in out]})
        return out


def _number_reads(reader, rois, kind, original="", reference_score=0, *, min_score=0):
    evidence = {}
    floor = max(reader.cfg["min_score"], reference_score - reader.cfg["score_drop"], min_score)
    for roi in rois:
        for det in reader.read(roi):
            clipped = tuple(_clip(roi, reader.bounds))
            if clipped[3] < reader.cfg["number_height"] / 2:
                continue
            number = det["text"]
            if not valid_number(number, kind, reader.cfg) or det["score"] < floor:
                continue
            if len(original) == 2 and len(number) < 2:
                continue
            entry = evidence.setdefault(number, {"rois": set(), "score": 0.0})
            entry["rois"].add(clipped)
            entry["score"] = max(entry["score"], det["score"])
    return evidence


def _choose_number(evidence, score_drop=0.25):
    # Crops are correlated. Require consistency, never outvote a competing number.
    if evidence:
        floor = max(entry["score"] for entry in evidence.values()) - score_drop
        evidence = {number: entry for number, entry in evidence.items() if entry["score"] >= floor}
    if len(evidence) != 1:
        return "", 0.0
    number, entry = next(iter(evidence.items()))
    if len({roi[1] for roi in entry["rois"]}) < 2:
        return "", 0.0
    return number, entry["score"]


def _fine_number_rois(reader, evidence):
    """Refine one strong but vertically unconfirmed candidate, at most four crops."""
    if len(evidence) != 1:
        return []
    number, entry = next(iter(evidence.items()))
    if entry["score"] < reader.cfg["initial_min_score"] or len({r[1] for r in entry["rois"]}) != 1:
        return []
    def score(roi):
        return max((d["score"] for d in reader.cache.get((*roi, True), [])
                    if d["text"] == number), default=0)
    x, y, w, h = max(sorted(entry["rois"]), key=score)
    rois = []
    for offset in (-1, 1, -2, 2):
        roi = [x, y + offset, w, h]
        # Do not change dimensions or cross into the monthly row during fine sampling.
        if _clip(roi, reader.bounds) == roi:
            rois.append(roi)
    return rois


def rescue_tail_num(context, screenshot, type_dets, cfg, bounds, number_dets=(), kind=""):
    if not type_dets or bounds is None:
        return "", 0.0
    reader = _Reader(context, screenshot, bounds, cfg)
    return _choose_number(_number_reads(reader, rescue_rois(type_dets, cfg), kind), cfg["score_drop"])


def _sweep(bounds, height, cfg, numeric):
    left, top, right, bottom = bounds
    if numeric:
        left += round((right - left) * cfg["number_left_frac"])
        right -= cfg["number_right_pad"]
        top += cfg["type_height"]
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
    reader = _Reader(context, screenshot, bounds, cfg)
    typed = [d for d in dets if re.search(r'[^\W\d_]', d["text"])]
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
        for roi in _sweep(bounds, cfg["type_height"], cfg, False):
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
                type_score = min(d["score"] for d in hits)
                meta["type_basis"] = "type_sweep"
        if not complete:
            meta["attempts"] = reader.attempts
            mfaalog.warning(f"[Arbitrage] 当前卡带类型未确认: {label}，不按数字猜类型")
            return "", 0.0, "unconfirmed_current_type", meta
    body = cfg["type_labels"][kind]
    meta["type"] = kind
    meta["geometry_basis"] = "single_line" if geometry else "default_height"
    # Inline numbers may belong to an otherwise correctly detected type line.
    inline = [re.search(r"([0-9]+)$", _clean(d["text"])) for d in typed]
    originals = [m.group(1) for m in inline if m] + [d["text"] for d in numbers]
    originals = list(dict.fromkeys(originals))
    original = originals[0] if len(originals) == 1 else ""
    number_score = min((d["score"] for d in numbers), default=type_score)
    wrapped = bool(numbers and typed and any(n["cy"] > max(t["cy"] for t in typed)
                    + min(t["h"] for t in typed) / 2 for n in numbers))
    ambiguous_one = wrapped and len(original) == 1 and valid_number("1" + original, kind, cfg)
    if (geometry and len(originals) == 1 and valid_number(original, kind, cfg)
            and number_score >= cfg["initial_min_score"] and not ambiguous_one):
        meta["number_basis"] = "initial_complete"
        meta["attempts"] = reader.attempts
        return body + original, min(type_score, number_score), "current_row", meta
    evidence = {}
    if geometry:
        evidence = _number_reads(reader, rescue_rois(normal, cfg), kind, original, number_score if original else 0)
    number, score = _choose_number(evidence, cfg["score_drop"])
    if not number:
        more = _number_reads(reader, _sweep(bounds, cfg["number_height"], cfg, True),
                             kind, original, number_score if original else 0)
        for token, entry in more.items():
            previous = evidence.setdefault(token, {"rois": set(), "score": 0.0})
            previous["rois"].update(entry["rois"])
            previous["score"] = max(previous["score"], entry["score"])
        number, score = _choose_number(evidence, cfg["score_drop"])
    if not number:
        more = _number_reads(reader, _fine_number_rois(reader, evidence), kind, original,
                             number_score if original else 0, min_score=cfg["initial_min_score"])
        for token, entry in more.items():
            previous = evidence.setdefault(token, {"rois": set(), "score": 0.0})
            previous["rois"].update(entry["rois"])
            previous["score"] = max(previous["score"], entry["score"])
        number, score = _choose_number(evidence, cfg["score_drop"])
    meta["attempts"] = reader.attempts
    meta["number_candidates"] = sorted(evidence)
    meta["number_scores"] = {token: round(entry["score"], 4) for token, entry in evidence.items()}
    if number:
        meta["number_basis"] = "number_rescue"
        mfaalog.info(f"[Arbitrage] 当前编号救援: {label} {original or '缺号'}→{number}")
        return body + number, min(type_score, score), "current_number_rechecked", meta
    reason = "number_conflict" if len(evidence) > 1 else "number_unreadable"
    meta["number_basis"] = reason
    mfaalog.warning(f"[Arbitrage] 当前编号未确认: {label}，原因={reason}，候选={sorted(evidence)}")
    return body, type_score, "unconfirmed_current_number", meta
