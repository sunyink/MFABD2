"""Resolve OCR item names against the shared catalog, without changing config names."""

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

from .ocr_score import select_best_ocr


def clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


@lru_cache(maxsize=1)
def item_aliases():
    data = Path(__file__).resolve().parents[1] / "data"
    items = json.loads((data / "bd2_item_names_i18n.json").read_text(encoding="utf-8"))
    allowed = {item["cn"] for item in items if item.get("category") in
               {"Ingredients", "Recipe", "CraftingMaterials", "MagicCrystals"}}
    # Shop shelves also contain skill books and other non-ingredient goods.
    # Recognize those before deciding they are not on the purchase list.
    shops = json.loads((data / "arbitrage_shop_catalog.json").read_text(encoding="utf-8"))
    allowed.update(name for shop in shops["cartridges"].values() for name in shop["items"])
    aliases = {clean(item[key]): item["cn"] for item in items if item.get("cn") in allowed
               for key in ("cn", "tw") if item.get(key)}
    aliases.update({clean(k): v for k, v in json.loads(
        (data / "bd2_name_norm_tw2cn.json").read_text(encoding="utf-8")).items() if v in allowed})
    return aliases


def name_candidates(text, aliases=None):
    aliases = item_aliases() if aliases is None else aliases
    text = clean(text)
    if text in aliases:
        return {aliases[text]}, "exact"
    if len(text) < 2:
        return set(), "short_unknown"
    # Collapse spelling aliases before deciding whether the item is unique.
    candidates = {canonical for alias, canonical in aliases.items() if len(alias) == len(text)
                  and sum(a != b for a, b in zip(alias, text)) == 1}
    return candidates, "one_character" if len(text) >= 3 else "short_candidate"


def resolve_ocr_name(text, score=1.0, reread=None, *, aliases=None, min_score=0.6):
    """Use a unique full-name match; bounded rereads handle ambiguity/weak OCR.

    The caller owns the image and crop geometry. This function never rewrites
    arbitrary user configuration or learns mutable aliases from an observation.
    """
    candidates, basis = name_candidates(text, aliases)
    result = {"raw": text, "name": clean(text), "confirmed": False,
              "basis": basis, "candidates": sorted(candidates)}
    if len(candidates) == 1 and score >= min_score and basis != "short_candidate":
        result.update(name=next(iter(candidates)), confirmed=True)
        return result
    if reread is not None:
        recovered = set()
        short_hits = {}
        for value, confidence in reread():
            choices, method = name_candidates(value, aliases)
            # A second fuzzy guess must not settle competing initial names.
            if confidence >= min_score and len(choices) == 1 and (
                    method == "exact" or not candidates):
                recovered.update(choices)
            elif confidence >= min_score and method == "short_candidate" and len(choices) == 1 and choices == candidates:
                name = next(iter(choices))
                short_hits[name] = short_hits.get(name, 0) + 1
        recovered.update(name for name, hits in short_hits.items() if hits >= 2)
        if len(recovered) == 1 and (not candidates or recovered <= candidates):
            result.update(name=next(iter(recovered)), confirmed=True, basis="local_reread")
    if not result["confirmed"]:
        result["basis"] = "unconfirmed_name"
    return result


def resolve_item_ocr(context, node, image, match):
    """Resolve a replaced OCR candidate, with two bounded local rereads if needed.

    The whole item catalog decides identity; the requested item is deliberately
    not an argument. All rereads use the same frame, so the click box and quantity
    evidence still describe the same screen.
    """
    candidate = select_best_ocr([match])
    if candidate is None:
        return {"raw": "", "name": "", "confirmed": False, "basis": "invalid_candidate",
                "candidates": []}

    def reread():
        shape = getattr(image, "shape", ())
        if len(shape) < 2:
            return
        height, width = shape[:2]
        x, y, w, h = candidate["box"]
        pad = max(1, round(h * .12))
        for delta in (0, pad):
            if getattr(getattr(context, "tasker", None), "stopping", False):
                return
            left, top = max(0, x - pad), max(0, y - pad + delta)
            right, bottom = min(width, x + w + pad), min(height, y + h + pad + delta)
            if right <= left or bottom <= top:
                continue
            result = context.run_recognition(node, image, {node: {
                "roi": [left, top, right - left, bottom - top], "roi_offset": [0, 0, 0, 0],
                "expected": [], "only_rec": True}})
            for item in getattr(result, "filtered_results", None) or []:
                value = select_best_ocr([item])
                if value is not None:
                    yield value["text"], value["score"]

    return resolve_ocr_name(candidate["text"], candidate["score"], reread)


def has_unconfirmed_item_name(detail):
    """Find item-name uncertainty through native Or/Custom detail wrappers."""
    if isinstance(detail, dict):
        return bool(detail.get("unconfirmed_names") or detail.get("name_read_failed")) or any(
            has_unconfirmed_item_name(value) for value in detail.values())
    if isinstance(detail, list):
        return any(has_unconfirmed_item_name(value) for value in detail)
    return False
