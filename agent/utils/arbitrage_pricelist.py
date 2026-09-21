"""Per-row price consistency and bounded local OCR shared by base and PC."""

from copy import deepcopy
import math
import re
import unicodedata

import numpy as np

from . import mfaalog
from .ocr_item_name import resolve_ocr_name


NAME_NODE = "Arbitrage_Sell_Name_Rescue"
AMOUNT_NODE = "Arbitrage_Sell_Amount_Rescue"
DEFAULT_CONFIG = {"initial_min_score": 0.8, "rescue_min_score": 0.6,
                  "white_min": 180, "white_chroma": 40, "white_column_pixels": 3,
                  "pad_fraction": 0.12, "rounding_tolerance": 1.0}
MONEY = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?P<sep>[,.])[0-9]{3}(?:(?P=sep)[0-9]{3})*)")


def load_config(context):
    cfg = deepcopy(DEFAULT_CONFIG)
    try:
        attach = getattr(context.get_node_object(AMOUNT_NODE), "attach", None) or {}
    except Exception:
        attach = {}
    for key, default in DEFAULT_CONFIG.items():
        value = attach.get(key, default)
        limit = 255 if key.startswith("white_") else 2
        if type(value) in (int, float) and math.isfinite(value) and 0 < value <= limit:
            cfg[key] = value
    return cfg


def money_value(text):
    text = unicodedata.normalize("NFKC", text or "").strip()
    pct = re.search(r"\d{2,3}\s*%", text)
    if pct:
        if re.search(r"[0-9%]", text[pct.end():]):
            return None
        text = text[:pct.start()]
    match = re.fullmatch(r"[^0-9.,%/+\-−–—]*([0-9]+(?:[,.][0-9]+)*)[^0-9.,%/+\-−–—]*", text)
    if not match or not MONEY.fullmatch(match[1]):
        return None
    value = int(re.sub(r"[,.]", "", match[1]))
    return value if value > 0 else None


def read_local(context, image, node, roi):
    """Keep every crop within the image; never resize or act on the controller."""
    if getattr(getattr(context, "tasker", None), "stopping", False):
        return []
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return []
    x, y, w, h = roi
    left, top = max(0, math.ceil(x)), max(0, math.ceil(y))
    right, bottom = min(image.shape[1], math.floor(x + w)), min(image.shape[0], math.floor(y + h))
    if right <= left or bottom <= top:
        return []
    roi = [left, top, right - left, bottom - top]
    try:
        result = context.run_recognition(node, image, pipeline_override={node: {
            "recognition": "OCR", "roi": roi, "only_rec": True}})
        return [(r.text, float(r.score)) for r in (getattr(result, "filtered_results", None) or [])]
    except Exception as exc:
        mfaalog.warning(f"[Arbitrage] 价目表局部OCR失败: {exc}")
        return []


def read_name(context, image, det):
    def reread():
        x, y, w, h = (det[k] for k in ("x", "y", "w", "h"))
        pad = max(1, round(h * .12))
        for delta in (0, pad):
            yield from read_local(context, image, NAME_NODE,
                                  [x - pad, y - pad + delta, w + 2 * pad, h + 2 * pad])
    result = resolve_ocr_name(det["text"], det.get("score", 1), reread)
    if result["confirmed"] and result["basis"] != "exact":
        mfaalog.info(f"[Arbitrage] 商品名救援: {det['text']}→{result['name']}")
    elif not result["confirmed"]:
        mfaalog.warning(f"[Arbitrage] 商品名未确认: {det['text']}")
    return result


def unique_amount(dets, min_score):
    values = {money_value(d["text"]) for d in dets}
    values.discard(None)
    if len(values) != 1:
        return None
    value = next(iter(values))
    score = max(d.get("score", 1) for d in dets if money_value(d["text"]) == value)
    return value if score >= min_score else None


def price_issues(row, tolerance=1.0):
    """Flag contradictions; never calculate a replacement price."""
    issues = {key for key in ("current_price", "peak_price", "current_rate", "peak_rate")
              if type(row.get(key)) is not int or row[key] <= 0}
    current, peak = row.get("current_price"), row.get("peak_price")
    rate, peak_rate, base = row.get("current_rate"), row.get("peak_rate"), row.get("base_price")
    for key in ("current_rate", "peak_rate"):
        if key not in issues and not 0 < row[key] < 1000:
            issues.add(key)
    if not issues:
        if peak < current or peak_rate < rate:
            issues.update(("current_price", "peak_price", "current_rate", "peak_rate"))
        # Integer display prices can coincide at different rates.
        if abs(peak * rate - current * peak_rate) > tolerance * (rate + peak_rate):
            issues.update(("current_price", "peak_price"))
    if type(base) is int and base > 0:
        for amount, percent in (("current_price", "current_rate"), ("peak_price", "peak_rate")):
            if amount not in issues and percent not in issues and abs(row[amount] - base * row[percent] / 100) > tolerance:
                issues.update((amount, "base_price"))
    return issues


def _white_extent(image, roi, cfg):
    if not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    x, y, w, h = map(int, roi)
    crop = image[max(0, y):max(0, y) + h, max(0, x):max(0, x) + w, :3].astype(np.int16)
    if not crop.size:
        return None
    white = (crop.min(axis=2) >= cfg["white_min"]) & (
        crop.max(axis=2) - crop.min(axis=2) <= cfg["white_chroma"])
    columns = np.flatnonzero(white.sum(axis=0) >= cfg["white_column_pixels"])
    if not len(columns):
        return None
    return max(0, x) + int(columns[0]), max(0, x) + int(columns[-1]) + 1


def _line_roi(column, center, height):
    x, y, w, h = column
    top, bottom = max(y, math.floor(center - height / 2)), min(y + h, math.ceil(center + height / 2))
    return [x, top, w, max(0, bottom - top)]


def confirm_prices(row):
    confirmed = row.get("price_read_basis") not in ("unconfirmed_price", "observation_conflict")
    values = [row.get(k) for k in ("current_price", "peak_price")]
    if confirmed and all(type(value) is int and value > 0 for value in values):
        row.update(is_max_price=values[0] == values[1], max_price_basis="amount")
    else:
        row.update(is_max_price=False, max_price_basis="unconfirmed_amount")


def read_prices(context, image, row, *, current, monthly, bases, centers, columns, cfg):
    """Normal OCR first; contradictory rows alone receive local rescue.

    Crops are anchored by the same row geometry as initial recognition. The
    upper white amount estimates a moving numeric band, not a fixed coin area.
    """
    row["current_price"] = unique_amount(current, cfg["initial_min_score"])
    row["peak_price"] = unique_amount(monthly, cfg["initial_min_score"])
    row["base_price"] = unique_amount(bases, cfg["initial_min_score"])
    issues = price_issues(row, cfg["rounding_tolerance"])
    evidence = {"initial_issues": sorted(issues)}
    row["price_evidence"] = evidence
    if not issues:
        row["price_read_basis"] = "initial_consistent"
        confirm_prices(row)
        return
    if centers[1] is None or not centers[0] < centers[1]:
        row["price_read_basis"] = "unconfirmed_price"
        confirm_prices(row)
        return
    gap = centers[1] - centers[0]
    height = max(8, gap * .82)
    rois = {key: _line_roi(columns["amount"], center, height)
            for key, center in zip(("current_price", "peak_price"), centers)}
    extent = _white_extent(image, rois["current_price"], cfg)
    pad = max(1, round(height * cfg["pad_fraction"]))
    attempts, candidates = [], {}
    for key in sorted(issues):
        if key == "base_price":
            if not bases:
                continue
            center = sum(d["cy"] for d in bases) / len(bases)
            proposals = [_line_roi(columns["name"], center, height)]
        elif key.endswith("rate"):
            center = centers[0 if key == "current_rate" else 1]
            proposals = [_line_roi(columns["rate"], center, height)]
        else:
            original = rois[key]
            proposals = []
            if extent:
                left, right = extent
                # Try measured width and a left expansion for a digit-count transition.
                for extra in (0, max(1, round(height * .6))):
                    start = max(original[0], left - pad - extra)
                    end = min(original[0] + original[2], right + pad)
                    proposals.append([start, original[1], end - start, original[3]])
            if not proposals:
                proposals = [original]
        found = set()
        for roi in proposals:
            reads = read_local(context, image, AMOUNT_NODE, roi)
            attempts.append({"field": key, "roi": roi, "reads": reads})
            for text, score in reads:
                if score < cfg["rescue_min_score"]:
                    continue
                if key.endswith("rate"):
                    match = re.fullmatch(r"\D*(\d{2,3})\s*%\D*", text.strip())
                    value = int(match[1]) if match else None
                else:
                    value = money_value(text)
                if value is not None:
                    found.add(value)
        candidates[key] = sorted(found)
    # Explore only the bounded set actually read from this row's image.
    from itertools import product
    keys = list(candidates)
    solutions = []
    choices = [candidates[k] or [None] for k in keys]
    if math.prod(len(c) for c in choices) <= 64:
        for values in product(*choices):
            trial = {**row, **dict(zip(keys, values))}
            if not price_issues(trial, cfg["rounding_tolerance"]):
                solutions.append(trial)
    signatures = {(s.get("current_price"), s.get("peak_price"), s.get("current_rate"), s.get("peak_rate"))
                  for s in solutions}
    evidence.update(candidates=candidates, attempts=attempts)
    if len(signatures) == 1:
        row.update({key: solutions[0].get(key) for key in
                    ("base_price", "current_price", "peak_price", "current_rate", "peak_rate")})
        if len({s.get("base_price") for s in solutions}) != 1:
            row["base_price"] = None
        row["price_read_basis"] = "local_rescue"
        mfaalog.info(f"[Arbitrage] 金额救援: {row['name']}，当前{row['current_price']}／月峰值{row['peak_price']}")
    else:
        row["price_read_basis"] = "unconfirmed_price"
        for key in issues:
            row[key] = None
        mfaalog.warning(f"[Arbitrage] 价目表金额未确认: {row['name']}，字段={','.join(sorted(issues))}")
    confirm_prices(row)
