"""OCR 读数的共用小工具：取识别文字、只认完整且唯一的数值。料理采集与背包仓检共用。"""

import re
import unicodedata

import numpy as np
from PIL import Image


NUMBER = r"(0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"


def clean(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def read_texts(context, node: str, image, retry: bool = False) -> tuple[list[str], int | None, bool]:
    if retry:
        config = context.get_node_object(node)
        scale = config.attach.get("retry_scale", 1)
        if not isinstance(scale, int) or not 1 < scale <= 4:
            return [], None, False
        x, y, width, height = config.recognition.param.roi
        crop = image[y:y + height, x:x + width]
        # 只缩放通道数值，不交换 BGR 顺序；不保存或改变原截图。
        enlarged = np.asarray(Image.fromarray(crop).resize(
            (width * scale, height * scale), Image.Resampling.BICUBIC,
        ))
        result = context.run_recognition(node, enlarged, {node: {"roi": [0, 0, width * scale, height * scale]}})
    else:
        result = context.run_recognition(node, image)
    if result is None:
        raise RuntimeError(f"OCR 节点未能执行: {node}")
    reco_id = getattr(result, "reco_id", None)
    if not result.hit:
        return [], reco_id, False
    items = getattr(result, "filtered_results", None) or getattr(result, "all_results", None) or []
    texts = []
    for item in items:
        text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        if text:
            texts.append(str(text))
    return texts, reco_id, True


def unique_parsed(texts: list[str], pattern: str):
    """只接受完整且唯一的读数；不猜斜杠、不拼接互相独立的 det 结果。"""
    values = set()
    for text in texts:
        match = re.fullmatch(pattern, clean(text))
        if match:
            values.add(tuple(int(value.replace(",", "")) for value in match.groups()))
    return next(iter(values)) if len(values) == 1 else None
