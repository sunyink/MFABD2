"""补买计划计算：纯输入输出，不读写存档、不操作界面、不把预测量写成实际量。"""

from datetime import date

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


def _offers(data, observations, day):
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
            observation = overrides.pop(key, None)
            if observation is None:
                price, remaining = item["price_reference"], item["daily_limit_reference"]
                source = "reference"
            else:
                price, remaining = observation.get("unit_price"), observation.get("remaining")
                source = "observed"
                if observation.get("day") != day:
                    issues.append({"shop_name": shop_name, "item_name": material, "reason": "stale_shop_observation"})
                    continue
            if type(price) is not int or price <= 0 or type(remaining) is not int or remaining < 0:
                issues.append({"shop_name": shop_name, "item_name": material, "reason": "unreadable_shop_offer"})
                continue
            if remaining:
                offers[key] = {"shop_name": shop_name, "item_name": material, "unit_price": price,
                               "remaining": min(remaining, _BATCH_LIMIT), "source": source}
    issues.extend({"shop_name": shop, "item_name": item, "reason": "unknown_shop_offer"} for shop, item in overrides)
    return offers, issues


def _fill(offers, quantity):
    lots = []
    cost = 0
    for offer in offers:
        amount = min(quantity, offer["remaining"])
        if amount:
            lots.append({"shop_name": offer["shop_name"], "item_name": offer["item_name"],
                         "quantity": amount, "unit_price": offer["unit_price"], "source": offer["source"]})
            cost += amount * offer["unit_price"]
            quantity -= amount
        if not quantity:
            break
    if quantity:
        raise ValueError("候选供给不足以填满计算出的数量")
    return lots, cost


def _candidate(recipe, material, available, prices, offers, budget, tonic_price):
    ingredients = recipe["ingredients"]
    needed_per = ingredients[material]
    owned = available[material]
    other_limits = [available[name] // count for name, count in ingredients.items() if name != material]
    if not other_limits:
        return None, "no_other_material_bound"
    other_limit = min(other_limits)
    # 最便宜柜台先填充。同一材料可跨柜台凑足一份，不提前买无法成份的零头。
    units, money = 0, budget
    boundaries = {1}
    for offer in offers:
        amount = min(offer["remaining"], money // offer["unit_price"])
        units += amount
        money -= amount * offer["unit_price"]
        boundary = (owned + units) // needed_per
        boundaries.update((boundary, boundary + 1))
    maximum = min(other_limit, (owned + units) // needed_per)
    if maximum < 1:
        return None, "insufficient_supply_or_budget"
    boundaries.add(maximum)
    best = None
    # 利润在各价格档之间是线性的；只检查档位两侧及端点，不逐份枚举大库存。
    for portions in sorted(n for n in boundaries if 1 <= n <= maximum):
        purchase_quantity = portions * needed_per - owned
        lots, cost = _fill(offers, purchase_quantity)
        stock_used = {name: (owned if name == material else portions * count)
                      for name, count in ingredients.items()}
        stock_value = sum(count * prices[name] for name, count in stock_used.items())
        tonic_count = portions * recipe["level"]
        revenue = portions * prices[recipe["name"]]
        gain = revenue - stock_value - cost - tonic_count * tonic_price
        if cost > budget or gain <= 0:
            continue
        candidate = {"portions": portions, "stock_used": stock_used, "purchase_quantity": purchase_quantity,
                     "lots": lots, "estimated_purchase_cost": cost, "estimated_stock_value": stock_value,
                     "estimated_peak_revenue": revenue, "estimated_tonic_count": tonic_count,
                     "estimated_tonic_cost": tonic_count * tonic_price, "estimated_gain": gain}
        if best is None or (gain, portions) > (best["estimated_gain"], best["portions"]):
            best = candidate
    return (best, "") if best else (None, "nonpositive_purchase_gain")


def build_replenish_plan(entries, quantities, market, data, *, day, budget, sell_names, shop_observations=()):
    """按P1队列顺序规划第一版单种缺料补买。

    quantities必须来自本次有效库存读口。商店无本轮观察时只生成参考报价候选，
    执行层仍需复核价格/余量；max_unit_price锁定本次计算使用的报价，不共享利润
    余量给多个柜台涨价。返回的unallocated_inventory是模型预留余额，不能写回存档。
    cook_today_candidates是本轮预定补做名单，不要求今天出售；成品留待峰值日出售。
    采购结束按顺序尝试，缺料由制作链跳过。
    """
    date.fromisoformat(day)
    _integer(budget, "补买预算")
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
              "budget_remaining": budget, "requests": [], "allocations": [], "skipped": [],
              "cook_today_candidates": [], "input_quantities": dict(available), "unallocated_inventory": dict(available)}
    if not isinstance(market, dict) or market.get("day") != day or market.get("complete") is not True:
        result.update(status="unavailable", reason="current_market_incomplete")
        return result
    prices = _market_prices(market)
    offers, result["offer_issues"] = _offers(data, shop_observations, day)
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
        candidate, reason = _candidate(recipe, material, available, prices, candidates, result["budget_remaining"], tonic_price)
        if candidate is None:
            skip(reason, material=material)
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
    result["requests"] = list(requests.values())
    result["estimated_spend"] = budget - result["budget_remaining"]
    result["unallocated_inventory"] = available
    return result
