"""买卖共用的白字整行报价解析；不拼接 OCR 块或推算划线原价。"""

import re
import unicodedata


COUNT = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"
MONEY = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:[,.][0-9]{3})+)"


def clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def parse_money(texts, cost=False):
    values = set()
    for raw in texts:
        text = clean(raw)
        if cost:
            text = re.sub(r"^(?:" + COUNT + r")?[个個]", "", text)
        if re.fullmatch(MONEY, text):
            values.add(int(text.replace(",", "").replace(".", "")))
    if len(values) != 1:
        raise ValueError(f"金额没有唯一完整读数: {texts!r}")
    return values.pop()


def parse_quote(items):
    texts = [item if isinstance(item, str) else item.get("text", "")
             if isinstance(item, dict) else getattr(item, "text", "") for item in items]
    if len(texts) != 1:
        raise ValueError(f"报价应为单条整行识别结果: {texts!r}")
    match = re.fullmatch(r"(" + COUNT + r")[个個](" + MONEY + r")\+?", clean(texts[0]))
    if match is None:
        raise ValueError(f"报价整行格式不符（数量+个+金额）: {texts!r}")
    quantity = int(match[1].replace(",", ""))
    if quantity < 1:
        raise ValueError("选量必须大于0")
    return quantity, parse_money([match[2]])
