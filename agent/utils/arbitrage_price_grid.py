"""Price-list cells drawn from the 当前 labels; base and PC share the logic.

Each 当前 starts one item. The single label box after it, whatever it reads,
is that item's 每月. Lines run through the middle of the gap between two boxes
(upper box bottom, lower box top), so no pixel tolerance is configured.
"""

import re
import unicodedata

from . import mfaalog


LABEL_NODE = "Arbitrage_Sell_Col_Label"
DEFAULT_PATTERNS = {"current_pattern": r"^.?前$"}


def clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def load_patterns(context):
    """Compiled label patterns from the node attach; invalid entries keep the defaults."""
    try:
        attach = getattr(context.get_node_object(LABEL_NODE), "attach", None) or {}
    except Exception:
        attach = {}
    patterns = {}
    for key, default in DEFAULT_PATTERNS.items():
        value = attach.get(key, default)
        try:
            patterns[key] = re.compile(value if isinstance(value, str) and value else default)
        except re.error:
            mfaalog.warning(f"[Arbitrage] 价目表标签正则无效({key})，改用默认")
            patterns[key] = re.compile(default)
    return patterns


def draw_rows(labels, top, bottom, patterns):
    """Return (rows, problem). Each row has current/monthly label boxes and top/mid/bottom lines.

    A missing 每月 is only accepted after the last 当前 (the row is cut at the
    list bottom); mid is then None and the caller must not trust its prices.
    """
    marks = sorted(labels, key=lambda t: (t["y"], t["x"]))
    starts = [i for i, t in enumerate(marks) if patterns["current_pattern"].search(clean(t["text"]))]
    if not starts:
        return [], "no_current"
    rows = []
    for k, i in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(marks)
        between = marks[i + 1:end]
        if len(between) > 1 or (not between and k + 1 < len(starts)):
            return [], f"labels_not_alternating:{k + 1}"
        rows.append({"current": marks[i], "monthly": between[0] if between else None})
    # 首个「当前」上方的框（表头，或对齐失败残留的上一页「每月」）同样按空隙正中画线，不必认字
    above = marks[:starts[0]]
    previous = max(above, key=lambda t: t["y"] + t["h"]) if above else None
    for row in rows:
        cur, mon = row["current"], row["monthly"]
        row["top"] = None if previous is None else (previous["y"] + previous["h"] + cur["y"]) / 2
        row["mid"] = None if mon is None else (cur["y"] + cur["h"] + mon["y"]) / 2
        previous = mon
    first, last = rows[0], rows[-1]
    if first["top"] is None:
        # 上方没有任何框：取「当前」到中位线的同一段距离对称上推；末行也缺「每月」时只能取列顶
        cur = first["current"]
        first["top"] = top if first["mid"] is None else max(top, cur["y"] - (first["mid"] - cur["y"] - cur["h"]))
    for k, row in enumerate(rows):
        row["bottom"] = rows[k + 1]["top"] if k + 1 < len(rows) else bottom
    if last["mid"] is None:
        # 被列底截断的末行：上格按「当前」框上下对称取，下格不判定
        cur = last["current"]
        last["upper_end"] = min(last["bottom"], cur["y"] + cur["h"] + (cur["y"] - last["top"]))
    for k, row in enumerate(rows):
        row.setdefault("upper_end", row["mid"])
        if not row["top"] < row["upper_end"] <= row["bottom"]:
            return [], f"bad_geometry:{k + 1}"
    return rows, None


def in_cell(dets, low, high):
    return [d for d in dets if low <= d["cy"] < high]


def join_cell(dets):
    """Join one cell's boxes left to right into a single detection; None when empty."""
    if not dets:
        return None
    ordered = sorted(dets, key=lambda d: d["x"])
    x = min(d["x"] for d in ordered)
    y = min(d["y"] for d in ordered)
    right = max(d["x"] + d["w"] for d in ordered)
    bottom = max(d["y"] + d["h"] for d in ordered)
    return {"text": "".join(d["text"] for d in ordered), "score": min(d.get("score", 1.0) for d in ordered),
            "x": x, "y": y, "w": right - x, "h": bottom - y, "cx": (x + right) / 2, "cy": (y + bottom) / 2}


def digits_only(dets):
    """Base-price cell: the position is already known, so every non-digit is dropped."""
    out = []
    for d in dets:
        digits = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", d["text"]))
        if digits:
            out.append({**d, "text": digits})
    return out
