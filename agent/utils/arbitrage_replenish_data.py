"""补买计划的静态配方/商店参考目录，不包含账号库存或可直接执行的采购指令。"""

import json
from pathlib import Path


DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "arbitrage_replenish.json"
PURCHASE_DISCOUNT_PERCENT = 60


def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label}必须是正整数")


def discounted_purchase_price(original_price):
    """普通商品按约定砍价60%，先将单价向下取整，再乘购买数量。"""
    _positive(original_price, "商品原价")
    return original_price * (100 - PURCHASE_DISCOUNT_PERCENT) // 100


def load_replenish_data(path=DATA_FILE):
    with Path(path).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("补买资料版本不支持")
    _positive(data.get("tonic_unit_price"), "神药参考单价")
    for key in ("recipes", "materials", "shops"):
        values = data.get(key)
        if not isinstance(values, dict) or not values or any(
            not isinstance(name, str) or not name.strip() or not isinstance(value, dict)
            for name, value in values.items()
        ):
            raise ValueError(f"补买资料{key}格式错误")
    for name, recipe in data["recipes"].items():
        _positive(recipe.get("level"), f"{name}星级")
        if recipe["level"] > 5:
            raise ValueError(f"{name}星级超出范围")
        _positive(recipe.get("peak_reference"), f"{name}峰值参考")
        ingredients = recipe.get("ingredients")
        if not isinstance(ingredients, dict) or not ingredients or not ingredients.keys() <= data["materials"].keys():
            raise ValueError(f"{name}配方不完整")
        for material, count in ingredients.items():
            _positive(count, f"{name}/{material}用量")
    for name, material in data["materials"].items():
        _positive(material.get("peak_reference"), f"{name}峰值参考")
    for name, shop in data["shops"].items():
        items = shop.get("items")
        if not isinstance(items, dict) or not items:
            raise ValueError(f"{name}采购目录为空")
        for material, item in items.items():
            if material not in data["materials"] or not isinstance(item, dict):
                raise ValueError(f"{name}包含未知材料")
            _positive(item.get("price_reference"), f"{name}/{material}参考价格")
            _positive(item.get("daily_limit_reference"), f"{name}/{material}参考供给")
    return data
