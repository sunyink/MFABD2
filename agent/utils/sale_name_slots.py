"""Locate sale-list cards by their type tags, so only name slots decide zero inventory.

The list ROI also holds rate badges, cooking star ratings, prices, sold-out badges
and section headings. Instead of filtering each new kind of noise, every card is
anchored by its type tag and only the box above the tag is read as its name.
"""

import re

from .ocr_item_name import clean


def slot_config(attach):
    """Validate the layout payload; a malformed value must stop zero inventory."""
    cfg = dict(attach or {})
    slot = cfg.get("slot")
    if (not isinstance(cfg.get("tag_pattern"), str) or not cfg["tag_pattern"]
            or not isinstance(cfg.get("name_node"), str) or not cfg["name_node"]
            or not isinstance(slot, list) or len(slot) != 4
            or any(type(value) is not int for value in slot) or slot[2] <= 0 or slot[3] <= 0):
        raise ValueError("出售名字框配置无效")
    for key in ("column_tolerance", "band_start"):
        if type(cfg.get(key)) is not int or cfg[key] < 0:
            raise ValueError(f"出售名字框配置无效：{key}")
    columns = cfg.get("columns")
    if not isinstance(columns, list) or not columns or any(type(x) is not int for x in columns):
        raise ValueError("出售名字框配置无效：columns")
    cfg["tag_re"] = re.compile(cfg["tag_pattern"])
    return cfg


def plan_slots(texts, list_roi, cfg):
    """Split whole-list OCR texts into name slots and column-aligned texts to check.

    ``texts`` are ``select_best_ocr`` dictionaries in image coordinates. Card columns
    are fixed per layout; a section heading spelled like a tag (ADB ``◆食物``) sits
    left of every column. A tag whose name centre is above the list top belongs to a
    card already read on the previous page. A name whose tag is below the list bottom,
    or whose tag OCR missed, is still checked: in a tagged row, or below the last row.
    """
    _left, top, _width, height = list_roi
    dx, dy, slot_w, slot_h = cfg["slot"]
    tolerance, columns = cfg["column_tolerance"], cfg["columns"]

    def on_column(text):
        return any(abs(text["box"][0] - column) <= tolerance for column in columns)

    tags = [text for text in texts if cfg["tag_re"].fullmatch(clean(text["text"])) and on_column(text)]

    slots, top_cut = [], 0
    for tag in tags:
        x, y = tag["box"][0] + dx, tag["box"][1] + dy
        if y + slot_h / 2 < top:
            top_cut += 1
            continue
        y_top = max(y, top)
        slots.append({"tag": tag, "roi": [x, y_top, slot_w, y + slot_h - y_top]})

    def inside(text, roi):
        cx = text["box"][0] + text["box"][2] / 2
        cy = text["box"][1] + text["box"][3] / 2
        return roi[0] <= cx < roi[0] + roi[2] and roi[1] <= cy < roi[1] + roi[3]

    # Without any tag the rows are unknown: check every column text, as the whole-list read did.
    last_row = max((tag["box"][1] for tag in tags), default=None)
    extra = []
    for text in texts:
        if text in tags or not on_column(text):
            continue
        cy = text["box"][1] + text["box"][3] / 2
        in_row = any(slot["roi"][1] <= cy < slot["roi"][1] + slot["roi"][3] for slot in slots)
        below = last_row is None or (cy >= last_row + cfg["band_start"] and text["box"][1] < top + height)
        if (in_row and not any(inside(text, slot["roi"]) for slot in slots)) or below:
            extra.append(text)
    return {"tags": tags, "slots": slots, "top_cut": top_cut, "extra": extra}
