"""Resolve OCR item names against the shared catalog, without changing config names."""

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata


def clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


@lru_cache(maxsize=1)
def item_aliases():
    data = Path(__file__).resolve().parents[1] / "data"
    items = json.loads((data / "bd2_item_names_i18n.json").read_text(encoding="utf-8"))
    allowed = {item["cn"] for item in items if item.get("category") in
               {"Ingredients", "Recipe", "CraftingMaterials", "MagicCrystals"}}
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
