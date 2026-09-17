"""按每份料理的基准多赚限制补买溢价；计算使用整数，展示时才换算每份金额。"""

from copy import deepcopy


DEFAULT_PROFIT_SURRENDER_PERCENT = 10


def validate_profit_surrender_percent(value):
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError("补买利润让出比例必须为0～100的整数")
    return value


def select_purchase(*, needed_per, owned, material_value, base_gain, offers, budget,
                    max_portions, percent, committed=()):
    """在价格档端点及比例边界选量；committed是已经确认买入、不能撤销的同配方采购。"""
    validate_profit_surrender_percent(percent)
    committed_quantity = sum(lot["quantity"] for lot in committed)
    committed_cost = sum(lot["quantity"] * lot["unit_price"] for lot in committed)
    committed_premium = sum(lot["quantity"] * max(0, lot["unit_price"] - material_value) for lot in committed)
    minimum = max(1, (owned + committed_quantity + needed_per - 1) // needed_per)
    units, premium, money = committed_quantity, committed_premium, max(0, budget - committed_cost)
    boundaries = {minimum}
    supply = []
    for offer in sorted(offers, key=lambda row: (row["unit_price"], row["shop_name"])):
        amount = min(offer["remaining"], money // offer["unit_price"])
        if not amount:
            continue
        supply.append({**offer, "remaining": amount})
        unit_premium = max(0, offer["unit_price"] - material_value)
        # 本价格档内：100*(每份用量*单价溢价*n + 截距) <= 比例*每份基准多赚*n。
        coefficient = 100 * needed_per * unit_premium - percent * base_gain
        intercept = premium - (owned + units) * unit_premium
        if coefficient:
            boundary = (-100 * intercept) // coefficient
            boundaries.update((boundary - 1, boundary, boundary + 1))
        units += amount
        premium += amount * unit_premium
        money -= amount * offer["unit_price"]
        boundary = (owned + units) // needed_per
        boundaries.update((boundary, boundary + 1))
    maximum = min(max_portions, (owned + units) // needed_per)
    if maximum < minimum or committed_cost > budget:
        return None, {"reason": "insufficient_supply_or_budget"}
    boundaries.add(maximum)
    best = rejected = None
    for portions in sorted(n for n in boundaries if minimum <= n <= maximum):
        quantity = portions * needed_per - owned - committed_quantity
        lots = []
        cost, premium = committed_cost, committed_premium
        for offer in supply:
            amount = min(quantity, offer["remaining"])
            if amount:
                lots.append({**{key: value for key, value in offer.items() if key != "remaining"}, "quantity": amount})
                cost += amount * offer["unit_price"]
                premium += amount * max(0, offer["unit_price"] - material_value)
                quantity -= amount
            if not quantity:
                break
        if quantity:
            continue
        bought = portions * needed_per - owned
        gain = portions * base_gain - (cost - bought * material_value)
        if cost > budget or gain <= 0:
            continue
        explanation = {"portions": portions, "base_gain_per_portion": base_gain, "premium_cost": premium}
        if premium * 100 > percent * portions * base_gain:
            if rejected is None or premium * rejected["portions"] < rejected["premium_cost"] * portions:
                rejected = explanation
            continue
        candidate = {**explanation, "lots": lots, "purchase_quantity": bought,
                     "estimated_purchase_cost": cost, "estimated_gain": gain}
        if best is None or (gain, portions) > (best["estimated_gain"], best["portions"]):
            best = candidate
    if best is not None:
        return best, {}
    if rejected is not None:
        return None, {"reason": "profit_surrender_exceeded", **rejected}
    return None, {"reason": "nonpositive_purchase_gain"}


def revalidate_requests(plan, requests, confirmed, blocked, remaining_budget, *, attempted=()):
    """按实际成交重分配尚未尝试的合规供给；可扩列，但不扩大原料理目标或重复进同柜台商品。"""
    source = plan.get("purchase_offers")
    if source is None:
        # 显式旧计划仅授权了请求内供给，不自行查默认目录扩列。
        source = [{"shop_name": row["shop_name"], "item_name": row["item_name"],
                   "unit_price": row["max_unit_price"], "remaining": row["target"]} for row in requests]
    pool = {(row["shop_name"], row["item_name"]): deepcopy(row) for row in source
            if (row["shop_name"], row["item_name"]) not in attempted}
    pending = {}
    money = remaining_budget
    decisions = []
    for allocation in plan["allocations"]:
        if allocation.get("kind") != "replenish":
            continue
        name = allocation["recipe"]
        basis = allocation["profit_basis"]
        paid = confirmed.get(name, [])
        paid_quantity = sum(lot["quantity"] for lot in paid)
        target = allocation["portions"] * basis["needed_per"] - basis["owned"]
        if paid_quantity >= target:
            continue
        offers = [row for row in pool.values() if row["item_name"] == basis["material"] and row["remaining"] > 0]
        previous = {row["shop_name"]: sum(use["quantity"] for use in row["uses"] if use["recipe"] == name)
                    for row in requests if any(use["recipe"] == name for use in row["uses"])}
        if name in blocked:
            candidate, detail = None, {"reason": "purchase_unconfirmed"}
        else:
            candidate, detail = select_purchase(
                needed_per=basis["needed_per"], owned=basis["owned"], material_value=basis["material_value"],
                base_gain=basis["base_gain_per_portion"], offers=offers,
                budget=money + sum(lot["quantity"] * lot["unit_price"] for lot in paid),
                max_portions=allocation["portions"], percent=plan["profit_surrender_percent"], committed=paid)
        allocated = {}
        if candidate is not None:
            for lot in candidate["lots"]:
                key = (lot["shop_name"], lot["item_name"])
                quantity = lot["quantity"]
                pool[key]["remaining"] -= quantity
                allocated[lot["shop_name"]] = quantity
                row = pending.setdefault(key, {"shop_name": key[0], "item_name": key[1], "target": 0,
                                               "max_unit_price": lot["unit_price"], "budget": 0, "uses": []})
                row["target"] += quantity
                row["budget"] += quantity * lot["unit_price"]
                row["uses"].append({"recipe": name, "quantity": quantity})
                money -= quantity * lot["unit_price"]
        if not allocated and not detail:
            detail = {"reason": "no_admissible_increment"}
        if allocated != previous or not allocated:
            decisions.append({"recipe": name, "before": sum(previous.values()), "after": sum(allocated.values()),
                              "material": basis["material"], "added_shops": sorted(allocated.keys() - previous.keys()),
                              "detail": detail or candidate})
    return list(pending.values()), decisions
