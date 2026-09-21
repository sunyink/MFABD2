"""Choose the most confident candidate from an already filtered OCR result.

Pass ``recognition.filtered_results`` to ``select_best_ocr``. The caller keeps
ownership of OCR matching, thresholds and image capture; rejected ``all_results``
must not be used as a fallback. Equal scores retain the source order.
"""

from collections.abc import Mapping
import math
from numbers import Integral, Real


def select_best_ocr(candidates):
    """Return a JSON-safe ``{box, score, text}`` winner, or None.

    Accept MaaFramework OCRResult objects or their dictionary representation.
    Do not mutate the input or impose another confidence threshold. Malformed
    candidates cannot supply a click target and are ignored.
    """
    best = None
    for item in candidates or ():
        read = item.get if isinstance(item, Mapping) else lambda key: getattr(item, key, None)
        score, text, box = read("score"), read("text"), read("box")
        if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score):
            continue
        if not 0 <= score <= 1 or not isinstance(text, str) or not text:
            continue
        if all(hasattr(box, key) for key in ("x", "y", "w", "h")):
            box = [box.x, box.y, box.w, box.h]
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        if any(isinstance(value, bool) or not isinstance(value, Integral) for value in box):
            continue
        if box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0:
            continue
        if best is None or score > best["score"]:
            best = {"box": [int(value) for value in box], "score": float(score), "text": text}
    return best
