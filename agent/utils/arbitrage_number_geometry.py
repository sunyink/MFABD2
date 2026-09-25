"""Locate complete current-row cartridge digits without OCR, input, or storage."""

from dataclasses import dataclass, field
import math

import numpy as np


GEOMETRY_VERSION = 1
# Match the PC startup gate's accepted aspect deviation after short-side resizing.
ASPECT_TOLERANCE = 0.02


@dataclass
class NumberRegion:
    detail: dict = field(default_factory=lambda: {
        "geometry_version": GEOMETRY_VERSION, "status": "geometry_unknown",
        "layout": "unknown", "reason": "invalid_image",
    })
    foreground: np.ndarray | None = field(default=None, repr=False)


def _runs(values, offset):
    result, start = [], None
    for index, value in enumerate(values):
        if value and start is None:
            start = index
        elif not value and start is not None:
            result.append([start + offset, index + offset])
            start = None
    if start is not None:
        result.append([start + offset, len(values) + offset])
    return result


def _otsu(gray):
    if not gray.size or gray.min() == gray.max():
        return None
    histogram = np.bincount(gray.ravel(), minlength=256).astype(float)
    probability = histogram / histogram.sum()
    weight = np.cumsum(probability)
    mean = np.cumsum(probability * np.arange(256))
    variance = (mean[-1] * weight - mean) ** 2 / np.maximum(weight * (1 - weight), 1e-12)
    return int(np.argmax(variance))


def analyze_number_region(image_bgr, current_bounds, type_dets, config):
    """Return JSON-safe geometry plus a transient row mask; all intervals are half-open."""
    region = NumberRegion()
    detail = region.detail
    if isinstance(image_bgr, np.ndarray):
        detail["image_shape"] = list(image_bgr.shape)
    if (not isinstance(image_bgr, np.ndarray) or image_bgr.dtype != np.uint8
            or image_bgr.ndim != 3 or image_bgr.shape[0] != 720 or image_bgr.shape[2] != 3
            or abs(image_bgr.shape[1] / 1280 - 1) > ASPECT_TOLERANCE):
        return region
    if (not isinstance(current_bounds, (tuple, list)) or len(current_bounds) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in current_bounds)):
        detail["reason"] = "invalid_bounds"
        return region
    left, top = math.ceil(current_bounds[0]), math.ceil(current_bounds[1])
    right, bottom = math.floor(current_bounds[2]), math.floor(current_bounds[3])
    detail["bounds"] = [left, top, right, bottom]
    if not (0 <= left < right <= image_bgr.shape[1] and 0 <= top < bottom <= image_bgr.shape[0]):
        detail["reason"] = "invalid_bounds"
        return region
    strip_left = left + round((right - left) * config["number_left_frac"])
    strip_right = right - config["number_right_pad"]
    if not left <= strip_left < strip_right <= right:
        detail["reason"] = "invalid_strip"
        return region
    pixels = image_bgr[top:bottom, left:right].astype(np.float64)
    gray = np.rint(.114 * pixels[:, :, 0] + .587 * pixels[:, :, 1] + .299 * pixels[:, :, 2]).astype(np.uint8)
    threshold = _otsu(gray[:, strip_left - left:strip_right - left])
    detail["strip"] = [strip_left, top, strip_right - strip_left, bottom - top]
    if threshold is None:
        detail["reason"] = "uniform_or_empty"
        return region
    region.foreground = gray > threshold
    bands = _runs(region.foreground[:, strip_left - left:strip_right - left].any(axis=1), top)
    detail.update(threshold=threshold, bands=bands)
    if len(bands) not in (1, 2):
        detail["reason"] = "ambiguous_bands"
        return region
    typed = bands[0]
    if bands[0][0] <= top or bands[-1][1] >= bottom:
        detail["reason"] = "touches_vertical_boundary"
        return region
    if not any(typed[0] <= d["cy"] < typed[1] for d in type_dets):
        detail["reason"] = "type_band_unanchored"
        return region
    detail["type_band"] = typed
    if len(bands) == 1:
        detail.update(status="inline_candidate", layout="inline", reason="one_band")
        return region
    numeric = bands[1]
    number_height = numeric[1] - numeric[0]
    low, high = config["number_band_height_ratio"]
    if not config["number_height"] * low <= number_height <= config["number_height"] * high:
        detail["reason"] = "number_height"
        return region
    blocks = _runs(region.foreground[numeric[0] - top:numeric[1] - top].any(axis=0), left)
    detail.update(layout="wrapped", number_band=numeric, blocks=blocks)
    if not blocks or blocks[0][0] <= left or blocks[-1][1] >= right:
        detail["reason"] = "touches_horizontal_boundary"
        return region
    if len(blocks) not in (1, 2):
        detail["reason"] = "ambiguous_digit_shape"
        return region
    detail.update(status="wrapped_bounded", reason="two_bands",
                  number_box=[blocks[0][0], numeric[0], blocks[-1][1] - blocks[0][0], number_height],
                  safe_y=sorted({typed[1], numeric[0]}))
    return region


def admit_number_crop(region, crop, config=None):
    """Reject clipped glyphs, other foreground, and anything outside the current row."""
    detail = region.detail
    if detail["status"] != "wrapped_bounded" or region.foreground is None:
        return {"admitted": False, "reason": "geometry_unknown"}
    if (not isinstance(crop, (list, tuple)) or len(crop) != 4
            or any(type(v) is not int for v in crop)):
        return {"admitted": False, "reason": "invalid_crop"}
    x, y, width, height = crop
    left, top, right, bottom = detail["bounds"]
    if width <= 0 or height <= 0 or not (left <= x < x + width <= right and top <= y < y + height <= bottom):
        return {"admitted": False, "reason": "out_of_bounds"}
    nx, ny, nw, nh = detail["number_box"]
    if not (x <= nx and nx + nw <= x + width and y <= ny and ny + nh <= y + height):
        return {"admitted": False, "reason": "cut_number"}
    if y < detail["type_band"][1]:
        return {"admitted": False, "reason": "includes_type"}
    ys, xs = np.where(region.foreground[y - top:y + height - top, x - left:x + width - left])
    if ((xs + x < nx) | (xs + x >= nx + nw) | (ys + y < ny) | (ys + y >= ny + nh)).any():
        return {"admitted": False, "reason": "extra_foreground"}
    return {"admitted": True, "reason": "complete_number"}


def build_number_crops(region, config):
    detail = region.detail
    if detail["status"] != "wrapped_bounded":
        return []
    left, _, right, bottom = detail["bounds"]
    nx, ny, nw, nh = detail["number_box"]
    specs, seen = [], set()
    for y in detail["safe_y"]:
        for fraction in config["number_crop_x_padding_fracs"]:
            padding = math.ceil(nh * fraction)
            x1, x2 = max(left, nx - padding), min(right, nx + nw + padding)
            y2 = min(bottom, ny + nh + config["number_crop_bottom_padding"])
            crop = [x1, y, x2 - x1, y2 - y]
            if tuple(crop) not in seen:
                seen.add(tuple(crop))
                specs.append({"roi": crop, **admit_number_crop(region, crop, config)})
    return specs
