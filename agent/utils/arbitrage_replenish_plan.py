"""补买计划计算：纯输入输出，不读写存档、不操作界面、不把预测量写成实际量。"""

from copy import deepcopy
from datetime import date

from .arbitrage_replenish_data import discounted_purchase_price
from .arbitrage_replenish_profit import DEFAULT_PROFIT_SURRENDER_PERCENT, select_purchase, validate_profit_surrender_percent
from .name_i18n import canon


_BATCH_LIMIT = 99999


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label}必须是大于等于{minimum}的整数")
    return value


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("物品或柜台名称无效")
    return canon(value.strip())


def order_purchase_requests(requests):
    """材料沿计划首次出现顺序；同材料严格按柜台单价从低到高执行。"""
    material_order = {}
    for row in requests:
        material_order.setdefault(row["item_name"], len(material_order))
    return sorted(requests, key=lambda row: (material_order[row["item_name"]], row["max_unit_price"], row["shop_name"]))


def _market_prices(market):
    prices, conflicts = {}, set()
    observations = {}
    for item in market.get("items", []):
        name = _name(item.get("name"))
        price = item.get("peak_price")
        if name in observations and observations[name] != price:
            conflicts.add(name)
        observations[name] = price
        if type(price) is not int or price <= 0:
            continue
        prices[name] = price
    for name in conflicts:
        prices.pop(name, None)
    return prices


def _offers(data, observations, day, purchased_items):
    overrides = {}
    for row in observations:
        key = (_name(row.get("shop_name")), _name(row.get("item_name")))
        if key in overrides:
            raise ValueError(f"同柜台商品观察重复: {key}")
        overrides[key] = row
    offers = {}
    issues = []
    for shop_name, shop in data["shops"].items():
        for material, item in shop["items"].items():
            key = (shop_name, material)
            price_limit = discounted_purchase_price(item["price_reference"])
            observation = overrides.pop(key, None)
            if observation is None:
                if key in purchased_items:
                    issues.append({"shop_name": shop_name, "item_name": material,
                                   "reason": "regular_purchase_completed"})
                    continue
                price, remaining = price_limit, item["daily_limit_reference"]
                source = "discounted_reference"
            else:
                price, remaining = observation.get("unit_price"), observation.get("remaining")
                source = "observed"
                if observation.get("day") != day:
                    issues.append({"shop_name": shop_name, "item_name": material, "reason": "stale_shop_observation"})
                    continue
            if type(price) is not int or price <= 0 or type(remaining) is not int or remaining < 0:
                issues.append({"shop_name": shop_name, "item_name": material, "reason": "unreadable_shop_offer"})
                continue
            if price > price_limit:
                issues.append({"shop_name": shop_name, "item_name": material,
                               "reason": "purchase_discount_not_met", "unit_price": price,
                               "max_unit_price": price_limit})
                continue
            if remaining:
                offers[key] = {"shop_name": shop_name, "item_name": material, "unit_price": price,
                               "remaining": min(remaining, _BATCH_LIMIT), "source": source}
    issues.extend({"shop_name": shop, "item_name": item, "reason": "unknown_shop_offer"} for shop, item in overrides)
    return offers, issues


def _candidate(recipe, material, available, prices, offers, budget, tonic_price, percent):
    ingredients = recipe["ingredients"]
    needed_per = ingredients[material]
    owned = available[material]
    other_limits = [available[name] // count for name, count in ingredients.items() if name != material]
    if not other_limits:
        return None, {"reason": "no_other_material_bound"}
    base_gain = prices[recipe["name"]] - sum(count * prices[name] for name, count in ingredients.items()) - recipe["level"] * tonic_price
    candidate, detail = select_purchase(
        needed_per=needed_per, owned=owned, material_value=prices[material], base_gain=base_gain,
        offers=offers, budget=budget, max_portions=min(other_limits), percent=percent)
    if candidate is None:
        return None, detail
    portions = candidate["portions"]
    stock_used = {name: (owned if name == material else portions * count) for name, count in ingredients.items()}
    candidate.update(stock_used=stock_used,
                     estimated_stock_value=sum(count * prices[name] for name, count in stock_used.items()),
                     estimated_peak_revenue=portions * prices[recipe["name"]],
                     estimated_tonic_count=portions * recipe["level"],
                     estimated_tonic_cost=portions * recipe["level"] * tonic_price,
                     profit_basis={"material": material, "needed_per": needed_per, "owned": owned,
                                   "material_value": prices[material], "base_gain_per_portion": base_gain})
    return candidate, {}


def build_replenish_plan(entries, quantities, market, data, *, day, budget, sell_names,
                         shop_observations=(), purchased_items=(),
                         profit_surrender_percent=DEFAULT_PROFIT_SURRENDER_PERCENT):
    """按P1队列顺序规划第一版单种缺料补买。

    quantities必须来自本次有效库存读口。商店无本轮观察时按原价减60%生成候选，
    现场报价同样不得超过约定折扣价。执行层仍需复核价格/余量；max_unit_price
    锁定本次计算使用的报价。每份补买溢价不得超过基准多赚的指定比例；跨柜台合计后
    按制作份数折算，不重复让出额度。unallocated_inventory是模型预留余额，不能写回存档。
    cook_today_candidates是本轮预定补做名单，不要求今天出售；成品留待峰值日出售。
    采购结束按顺序尝试，缺料由制作链跳过。
    """
    date.fromisoformat(day)
    _integer(budget, "补买预算")
    validate_profit_surrender_percent(profit_surrender_percent)
    if isinstance(sell_names, (str, bytes)):
        raise ValueError("待售菜谱必须为名称集合")
    sell_names = {_name(name) for name in sell_names}
    available = {}
    for raw_name, value in quantities.items():
        name = _name(raw_name)
        _integer(value, f"{name}库存")
        if name in available:
            raise ValueError(f"库存别名重复: {name}")
        available[name] = value
    result = {"status": "planned", "day": day, "budget": budget, "estimated_spend": 0,
              "profit_surrender_percent": profit_surrender_percent,
              "budget_remaining": budget, "requests": [], "allocations": [], "skipped": [],
              "cook_today_candidates": [], "input_quantities": dict(available), "unallocated_inventory": dict(available)}
    if not isinstance(market, dict) or market.get("day") != day or market.get("complete") is not True:
        result.update(status="unavailable", reason="current_market_incomplete")
        return result
    prices = _market_prices(market)
    offers, result["offer_issues"] = _offers(data, shop_observations, day, set(purchased_items))
    # 保留分配前的合规供给，售罄后可在原料理目标内补位；不得从默认目录恢复已排除供给。
    result["purchase_offers"] = deepcopy(list(offers.values()))
    tonic_price = _integer(data["tonic_unit_price"], "神药参考单价", 1)
    requests = {}
    seen = set()
    for entry in entries:
        name = entry.name
        def skip(reason, **detail):
            result["skipped"].append({"recipe": name, "entry": entry.entry, "reason": reason, **detail})
        if not name or entry.reason or not entry.enabled:
            skip("recipe_not_enabled_or_resolved")
            continue
        if name in seen:
            raise ValueError(f"重复的可执行菜谱: {name}")
        seen.add(name)
        if name not in sell_names:
            skip("recipe_sale_not_permitted")
            continue
        source = data["recipes"].get(name)
        if source is None:
            skip("recipe_data_missing")
            continue
        recipe = {**source, "name": name}
        ingredients = recipe["ingredients"]
        missing = sorted(set(ingredients) - available.keys())
        if missing:
            skip("inventory_unknown", materials=missing)
            continue
        missing_prices = sorted(({name} | ingredients.keys()) - prices.keys())
        if missing_prices:
            skip("peak_price_unknown", items=missing_prices)
            continue
        if prices[name] - sum(count * prices[material] for material, count in ingredients.items()) - recipe["level"] * tonic_price <= 0:
            skip("cooking_not_better_than_raw_sale")
            continue
        shortages = [material for material, count in ingredients.items() if available[material] < count]
        if not shortages:
            # 已经可做的菜不产生补买；预留其存量用途，避免再给后面的菜重复估算。
            portions = min(available[material] // count for material, count in ingredients.items())
            stock_used = {material: portions * count for material, count in ingredients.items()}
            for material, count in stock_used.items():
                available[material] -= count
            result["allocations"].append({"recipe": name, "kind": "existing", "portions": portions, "stock_used": stock_used})
            skip("already_craftable")
            continue
        if len(shortages) != 1:
            skip("multiple_missing_materials", materials=shortages)
            continue
        material = shortages[0]
        candidates = sorted((offer for offer in offers.values() if offer["item_name"] == material and offer["remaining"]),
                            key=lambda offer: (offer["unit_price"], offer["shop_name"]))
        if not candidates:
            skip("no_purchase_offer", material=material)
            continue
        candidate, detail = _candidate(recipe, material, available, prices, candidates, result["budget_remaining"],
                                       tonic_price, profit_surrender_percent)
        if candidate is None:
            skip(detail["reason"], material=material, **{key: value for key, value in detail.items() if key != "reason"})
            continue
        for lot in candidate["lots"]:
            key = (lot["shop_name"], material)
            offers[key]["remaining"] -= lot["quantity"]
            if key not in requests:
                requests[key] = {"shop_name": lot["shop_name"], "item_name": material, "target": 0,
                                 "max_unit_price": lot["unit_price"], "budget": 0, "quote_source": lot["source"], "uses": []}
            request = requests[key]
            request["target"] += lot["quantity"]
            request["budget"] += lot["quantity"] * lot["unit_price"]
            request["uses"].append({"recipe": name, "quantity": lot["quantity"]})
        for ingredient, count in candidate["stock_used"].items():
            available[ingredient] -= count
        result["budget_remaining"] -= candidate["estimated_purchase_cost"]
        result["allocations"].append({"recipe": name, "entry": entry.entry, "kind": "replenish", **candidate})
        result["cook_today_candidates"].append(name)
    result["requests"] = order_purchase_requests(list(requests.values()))
    result["estimated_spend"] = budget - result["budget_remaining"]
    result["unallocated_inventory"] = available
    return result
