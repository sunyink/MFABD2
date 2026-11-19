"""库存记账：从账制表——某一个游戏日的报表模型。

纯函数，只吃账（inventory_ledger.build_ledger 的结果），不回头看凭证、不读写文件。
归日：一日的账只算到当天最后一盘——期初是当天第一盘，期末是当天最后一盘，各列只收这两盘之间的区间。
下一个游戏日的盘点还没来时只能记账（期中）；来了就结账（期末），结账用的就是最后一次记账的数，不拿次日的读数改它（U76）。
结转：结账定稿的表带一份期末余额（balance），每样东西一行，当天没读到的沿用上一张表的数。下一日的期初是它自己第一盘
读到的数，与上日期末的差额（隔夜那一段的变化、没读准后来补上的）全部记成期初调整，定稿的表不回头改（U75）。
"""

from .inventory_ledger import CURRENCY_SCOPE, SUSPECT_MIN, items_until, ts

SCHEMA = "ledger-report/2"
KIND_LABEL = {"daily": "日常", "collect": "吸取", "shop": "商店", "outside": "期间外", "mixed": "混合", "later": "次日以后"}
GROUPS = (("money", "货币票券"), ("ingredient", "存货 · 食材"), ("dish", "存货 · 料理"), ("growth", "养成材料"),
          ("other", "其他"))
GROUP_OF = {"currency": "money", "ticket": "money", "ingredient": "ingredient", "dish": "dish", "growth": "growth",
            "craft": "growth"}
VALUED = ("ingredient", "dish")
GOLD = "金币"
REASONS = {"unknown": "中间有盘点没读到它", "unread": "中间有盘点没读到顶栏", "out_of_scope": "中间的盘点不扫这一科目",
           "missed": "中间有盘点没盘完", "suspect_zero": "中间那次盘点把它判成了 0，前后读数相同，按漏读处理"}


def _unit(prices, scan, name):
    if not prices:
        return None
    row = (prices.get(scan["scan_id"]) or {}).get(name)
    return row if isinstance(row, dict) and type(row.get("unit")) is int else None


def _balance_row(quantity, at, price=None) -> dict:
    """期末余额里的一行：数量、读到它的那次盘点的时刻，有单价时带上单价。"""
    row = {"quantity": quantity, "at": at}
    if price:
        row["unit"] = price["unit"]
    return row


def _ledger_prior(ledger, first, prices) -> dict:
    """手上没有上一张定稿的表时，从账里推它会写的期末余额：上一日最后一盘为止，每样东西最后一次读到的数。

    只看到上一日最后一盘为止的账（items_until），与那张表定稿时看到的一样。
    """
    scans = ledger["scans"]
    if first <= 0:
        return {}
    result = {}
    for name, entry in items_until(ledger, first - 1).items():
        index, quantity = entry["readings"][-1]
        valued = prices is not None and GROUP_OF.get(entry["category"], "other") in VALUED
        result[name] = _balance_row(quantity, scans[index]["at"], _unit(prices, scans[index], name) if valued else None)
    return result


def _gap_reason(name, entry, origin, today) -> str:
    """上日期末与本日第一次读到的数不一样，是哪一种。today：本日里排在它第一次读数之前、没读到它的那几次盘点。

    本日第一盘就读到了（today 为空）是最平常的一种：差额就是隔夜那一段的变化。否则还并进了本日开头的一截，
    按没读到的原因分，口径同 inventory_ledger._chains。
    """
    doubted = {miss["scan"] for miss in entry.get("suspect") or ()}
    scope = entry.get("scope")
    if origin in doubted or any(scan["id"] in doubted for scan in today):
        return "suspect_zero"   # 上一张表上的 0（或本日开头的 0）夹在两次相同的读数之间：漏读
    if not today:
        return "overnight"
    if any(name in scan["unknown"] for scan in today):
        return "unknown"
    if scope == CURRENCY_SCOPE:
        return "unread"
    return "out_of_scope" if all(scope not in scan["scope"] for scan in today) else "missed"


def _items(ledger, chains, first, prices, prior):
    """逐物品：当天的期初、各列变动、跨区间合并、期末，以及（给了单价时）量价分解。

    chains：只看到当天最后一盘为止的读数链（items_until）。期初是当天第一次读到的数，期末是当天最后一次读到的数。
    上日期末看 prior——上一张定稿表的期末余额 {物品: {"quantity", "at", "unit"}}，不回头翻更早的凭证（U75）；
    它与本日期初的差额记成期初调整，单列，不进各列与损益（U67-1、U76）。
    """
    scans, intervals = ledger["scans"], ledger["intervals"]
    by_time = {scan["at"]: index for index, scan in enumerate(scans)}
    rows, unread = [], []
    for name, entry in chains.items():
        readings = [(index, quantity) for index, quantity in entry["readings"] if index >= first]
        if not readings:
            unread.append(name)
            continue
        pieces = [piece for piece in entry["pieces"] if piece["from"] >= first]
        opening_at, opening = readings[0]
        carried = prior.get(name)   # 上日期末；与本日期初对得上就没有要调整的
        if carried is not None and carried["quantity"] == opening:
            carried = None
        closing_at, closing = (pieces[-1]["to"], pieces[-1]["close"]) if pieces else readings[-1]
        group = GROUP_OF.get(entry["category"], "other")
        valued = group in VALUED and prices is not None
        price_open = _unit(prices, scans[opening_at], name) if valued else None
        price = _unit(prices, scans[closing_at], name) if valued else None
        deltas, spans, flows, reval, priced = {}, [], {}, 0, valued and price_open is not None and price is not None
        for piece in pieces:
            kinds = list(dict.fromkeys(intervals[index]["kind"] for index in range(piece["from"], piece["to"])))
            before, after = (_unit(prices, scans[piece["from"]], name), _unit(prices, scans[piece["to"]], name)) \
                if valued else (None, None)
            priced = priced and before is not None and after is not None
            flow = piece["delta"] * after["unit"] if priced else None
            if priced:
                reval += piece["open"] * (after["unit"] - before["unit"])
            if len(kinds) == 1:
                deltas[kinds[0]] = deltas.get(kinds[0], 0) + piece["delta"]
                if priced:
                    flows[kinds[0]] = flows.get(kinds[0], 0) + flow
            elif piece["delta"]:   # 合起来没变的不列：各段无从分开，也没有可列的数
                spans.append({"cols": kinds, "delta": piece["delta"], "from": scans[piece["from"]]["id"],
                              "to": scans[piece["to"]]["id"], "reason": piece.get("reason") or "cross_day",
                              "flow": flow})
        value = None
        if priced:
            value = {"opening": opening * price_open["unit"], "closing": closing * price["unit"], "flows": flows,
                     "spans": sum(span["flow"] for span in spans), "reval": reval, "adjust": 0}
        elif valued and not opening and not closing and carried is None \
                and not any(piece["open"] or piece["close"] for piece in pieces):
            priced = True   # 一直是 0 的东西没有单价也不碍事：估值就是 0，不必提醒
            value = {"opening": 0, "closing": 0, "flows": {}, "spans": 0, "reval": 0, "adjust": 0}
        adjust, gaps, origin = None, [], None
        if carried is not None:
            # 上次读到它的那一盘：还在这次装载的凭证里就按下标取中间的盘点，否则按时刻取。
            source = by_time.get(carried.get("at"))
            if source is not None:
                origin, gaps = scans[source]["id"], scans[source + 1:opening_at]
            else:
                since = ts(carried.get("at"))
                gaps = [scan for scan in scans[:opening_at] if since is None or scan["ts"] > since]
            today = scans[first:opening_at]   # 本日里排在它第一次读数之前的盘点
            worth, unit_before = None, None
            if value is not None and price_open is not None:
                # 期初的估值按调整后的期初；与上日期末的估值之差就是调整的金额（含两次单价之差）。
                unit_before = {"unit": carried["unit"]} if type(carried.get("unit")) is int else price_open
                worth = value["adjust"] = value["opening"] - carried["quantity"] * unit_before["unit"]
            adjust = {"before": carried["quantity"], "before_at": origin, "before_time": carried.get("at"),
                      "price_before": unit_before, "delta": opening - carried["quantity"],
                      "reason": _gap_reason(name, entry, origin, today),
                      "missing_at": [scan["id"] for scan in gaps], "value": worth}
        # 疑似漏读的 0 是按全部历史认的；只报落在本日各段（含期初调整那一段）中间的，别的日子不再带出。
        inside = {scans[index]["id"] for piece in pieces for index in range(piece["from"] + 1, piece["to"])}
        inside |= {scan["id"] for scan in gaps} | ({origin} if origin else set())
        suspect = [miss for miss in entry.get("suspect") or [] if miss["scan"] in inside]
        # 一大笔库存在一段里清零、这一段又没有它的出售或制作记录：多半是盘点漏读，提醒但不改数。
        emptied = next((piece["open"] for piece in pieces
                        if piece["to"] - piece["from"] == 1 and piece["open"] >= SUSPECT_MIN and piece["close"] == 0
                        and name not in intervals[piece["from"]]["book"]["sales"]
                        and not intervals[piece["from"]]["book"]["cooked"]), None)
        rows.append({
            "name": name, "category": entry["category"], "group": group, "kind": entry.get("kind"),
            "scope": entry.get("scope"), "opening": opening, "closing": closing, "total": closing - opening,
            "opening_at": scans[opening_at]["id"], "closing_at": scans[closing_at]["id"],
            "deltas": deltas, "spans": spans, "price": price, "price_open": price_open, "value": value,
            "unpriced": valued and not priced, "emptied": emptied, "suspect": suspect,
            "adjust": adjust})
    return rows, sorted(unread)


def _columns(day_intervals, scans_by_id):
    cols = {}
    for interval in day_intervals:
        col = cols.setdefault(interval["kind"], {"key": interval["kind"], "label": KIND_LABEL[interval["kind"]], "runs": 0,
                                                 "minutes": 0, "flags": [], "scopes": [], "intervals": []})
        col["runs"] += 1
        col["minutes"] += interval["minutes"]
        col["intervals"].append(interval["id"])
        col["flags"] = sorted(set(col["flags"]) | set(interval["flags"]))
        both = set(scans_by_id[interval["from"]]["scope"]) & set(scans_by_id[interval["to"]]["scope"])
        col["scopes"] = sorted(set(col["scopes"]) | both)
    return list(cols.values())


def _merge_books(day_intervals, kind="shop"):
    book = {"sales": {}, "replenish": {}, "cooked": {}, "unconfirmed": 0}
    stray = []
    for interval in day_intervals:
        source = interval["book"]
        if interval["kind"] != kind:
            if source["sales"] or source["replenish"] or source["cooked"]:
                stray.append(interval["id"])
            continue
        book["unconfirmed"] += source["unconfirmed"]
        for section in ("sales", "replenish", "cooked"):
            for name, row in source[section].items():
                target = book[section].setdefault(name, dict.fromkeys(row, 0))
                for key, value in row.items():
                    target[key] += value
    return book, stray


def _shop(items, book, cookbook):
    """商店账实核对：盘点净额 − 已记账（补买、出售）− 推算（制作）= 余额项（常规采购）。"""
    recipes = (cookbook or {}).get("recipes") or {}
    purchasable = (cookbook or {}).get("purchasable")
    by_name = {item["name"]: item for item in items}
    used, basis = {}, {}
    for dish, row in book["cooked"].items():
        for name, amount in (recipes.get(dish) or {}).items():
            used[name] = used.get(name, 0) + amount * row["made"]
            basis.setdefault(name, []).append(f"{dish} {row['made']} 份 × {amount}")
    names = set(book["sales"]) | set(book["replenish"]) | set(book["cooked"]) | set(used) | {
        item["name"] for item in items if item["group"] in VALUED and item["deltas"].get("shop")}
    rows = []
    for name in names:
        item = by_name.get(name)
        net = item["deltas"].get("shop") if item else None
        bought = book["replenish"].get(name, {}).get("quantity", 0)
        sold = book["sales"].get(name, {}).get("quantity", 0)
        made = book["cooked"].get(name, {}).get("made", 0)
        cooking = made - used.get(name, 0)
        residual = None if net is None else net - bought + sold - cooking
        notes = list(basis.get(name, ()))
        mismatch = False
        if name in book["cooked"]:
            row = book["cooked"][name]
            derived = None if net is None else net + sold
            notes.append(f"成品：记下制作 {made} 份" + ("" if derived is None else f"，两头读数加卖出推得 {derived} 份"))
            if row["inconsistent"]:
                notes.append(f"另有 {row['inconsistent']} 次制作读数对不上，未计入")
            mismatch = derived is not None and derived != made and not row["inconsistent"]
        elif purchasable is not None and name not in purchasable and residual:
            notes.append("不可购买，余额项应为 0")
            mismatch = True
        if bought:
            notes.append(f"补买 {bought}（花费 {book['replenish'][name]['spent']:,}）")
        if not (net or bought or sold or cooking or notes):
            continue   # 柜台上确认过「没有可卖的」也会留一条数量为 0 的出售记录：没有任何数可列，不占一行
        rows.append({"name": name, "net": net, "replenish": bought or None, "sales": -sold if sold else None,
                     "cooking": cooking or None, "residual": residual, "mismatch": mismatch,
                     "dish": name in recipes or name in book["cooked"], "basis": "；".join(notes)})
    rows.sort(key=lambda row: (not row["dish"], -abs(row["net"] or 0), row["name"]))
    gold = by_name.get(GOLD)
    net = gold["deltas"].get("shop") if gold else None
    sales = sum(row["gold"] for row in book["sales"].values())
    spent = sum(row["spent"] for row in book["replenish"].values())
    residual = None if net is None else net - sales + spent
    unknown = sorted(name for name, row in book["sales"].items() if row.get("unknown"))
    # 余额项只该是常规采购：它只花钱，不会为正；有出售没核到金币时，余额项里混着那几笔收入，也不能叫常规采购。
    return {"gold": {"net": net, "sales": sales, "replenish": -spent, "residual": residual,
                     "unconfirmed_sales": book["unconfirmed"],
                     "carried": sum(row.get("carried", 0) for row in book["sales"].values()),
                     "unknown_sales": unknown,
                     "regular": residual is not None and residual <= 0 and not unknown},
            "rows": rows, "cooked": [{"recipe": dish, **row} for dish, row in sorted(book["cooked"].items())],
            "mismatch": [row["name"] for row in rows if row["mismatch"]]}


def _by_card(ledger, chains, first, day_intervals, prices):
    """吸取分卡产出：各张卡带列它自己那几次进卡读到的数量，盘点与读数的差单列「卡带未知」；同一天同一张卡合成一行。

    一样东西在吸取段里的一笔变化（它物品链上的一段，可能跨几段吸取）：各卡照读到的数列，多了少了都不缩放；
    这笔减去各卡读数之和归「卡带未知」，可正可负——走路碰到的、没读到的、吸完却没记录的卡，说不清是哪张卡的就不替它分（U60）。
    盘点没给出变化的东西（不扫、没读准、跨到别的段）只有读数，单列、不估值。当天没有吸取段返回 None。
    """
    scans = ledger["scans"]
    collects = {first + offset: interval for offset, interval in enumerate(day_intervals) if interval["kind"] == "collect"}
    if not collects:
        return None
    cards, reads, full = {}, {}, {}

    def row(card):
        return cards.setdefault(card, {"card": card, "visits": 0, "complete": 0, "points": 0, "unread": 0, "stuck": 0,
                                       "finished": False, "items": {}, "worth": {}, "read_only": {}})

    for index, interval in collects.items():
        for card in interval["cards"] or ():
            row(card)["finished"] = True
        visits, named = interval.get("loot") or [], set()
        for visit in visits:
            target = row(visit["card"])
            target["visits"] += 1
            target["complete"] += visit["end"] == "complete"
            for key in ("points", "unread", "stuck"):
                target[key] += visit[key]
            named.add(visit["card"])
            for name, count in visit["items"].items():
                bucket = reads.setdefault((index, name), {})
                bucket[visit["card"]] = bucket.get(visit["card"], 0) + count
        # 这一段吸完的卡是不是都有掉落记录：只用来在页面上说明「卡带未知」里有没有整张卡的产出，不影响怎么分。
        full[index] = bool(visits) and interval["cards"] is not None and set(interval["cards"]) <= named

    def worth(name, quantity, scan):
        if prices is None:
            return None
        if name == GOLD:
            return quantity
        group = GROUP_OF.get(str((chains.get(name) or {}).get("category") or "other"), "other")
        unit = _unit(prices, scan, name) if group in VALUED else None
        return None if unit is None else quantity * unit["unit"]

    def add(target, name, quantity, value):
        target["items"][name] = target["items"].get(name, 0) + quantity
        known = target["worth"].get(name, 0)
        target["worth"][name] = None if value is None or known is None else known + value

    rest, used = {"card": None, "items": {}, "worth": {}}, set()
    for name, entry in chains.items():
        for piece in entry["pieces"]:
            span = range(piece["from"], piece["to"])
            if not all(index in collects for index in span):
                continue
            share = {}
            for index in span:
                used.add((index, name))
                for card, count in reads.get((index, name), {}).items():
                    share[card] = share.get(card, 0) + count
            closing = scans[piece["to"]]
            for card, quantity in share.items():
                if quantity:
                    add(row(card), name, quantity, worth(name, quantity, closing))
            left = piece["delta"] - sum(share.values())   # 盘点与读数的差，可正可负
            if left:
                add(rest, name, left, worth(name, left, closing))
    for (index, name), share in reads.items():
        if (index, name) not in used:
            for card, count in share.items():
                target = row(card)
                target["read_only"][name] = target["read_only"].get(name, 0) + count

    def listed(target):
        items = [{"name": name, "quantity": quantity, "value": target["worth"].get(name)}
                 for name, quantity in target["items"].items() if quantity]
        items.sort(key=lambda item: (item["name"] != GOLD, -(item["value"] or 0), -item["quantity"], item["name"]))
        values = [item["value"] for item in items]
        return items, (None if prices is None else sum(value for value in values if value is not None)), \
            sorted(item["name"] for item in items if item["value"] is None and prices is not None)

    rows = []
    for target in cards.values():
        items, value, unpriced = listed(target)
        rows.append({key: target[key] for key in ("card", "visits", "complete", "points", "unread", "stuck", "finished")}
                    | {"recorded": target["visits"] > 0, "items": items, "value": value, "unpriced": unpriced,
                       "read_only": [{"name": name, "quantity": quantity}
                                     for name, quantity in sorted(target["read_only"].items(), key=lambda pair: -pair[1])]})
    rows.sort(key=lambda row: (not row["recorded"], row["card"] is None, -(row["value"] or 0),
                               -sum(item["quantity"] for item in row["items"]), str(row["card"])))
    items, value, unpriced = listed(rest)
    return {"rows": rows, "unallocated": {"items": items, "value": value, "unpriced": unpriced},
            "runs": len(collects), "recorded": sum(bool(interval.get("loot")) for interval in collects.values()),
            "covered": sum(full.values())}


def _by_card_check(items, by_card):
    """C8：逐物品，各卡读到 + 卡带未知 = 表一的吸取列。"""
    allocated = {}
    for row in by_card["rows"] + [by_card["unallocated"]]:
        for entry in row["items"]:
            allocated[entry["name"]] = allocated.get(entry["name"], 0) + entry["quantity"]
    column = {item["name"]: item["deltas"].get("collect") or 0 for item in items}
    broken = sorted(name for name in set(allocated) | set(column) if allocated.get(name, 0) != column.get(name, 0))
    return {"name": "C8 各卡读到 + 卡带未知 = 吸取列（逐物品）", "ok": not broken,
            "detail": f"{len(by_card['rows'])} 张卡" if not broken else "不平：" + "、".join(broken[:8])}


def _valuation(items):
    result = {}
    for group in VALUED:
        rows = [item for item in items if item["group"] == group]
        priced = [item["value"] for item in rows if item["value"]]
        flows = {}
        for value in priced:
            for col, amount in value["flows"].items():
                flows[col] = flows.get(col, 0) + amount
        result[group] = {"opening": sum(value["opening"] for value in priced),
                         "closing": sum(value["closing"] for value in priced), "flows": flows,
                         "spans": sum(value["spans"] for value in priced),
                         "reval": sum(value["reval"] for value in priced),
                         "adjust": sum(value.get("adjust", 0) for value in priced),
                         "unpriced": sorted(item["name"] for item in rows if item["unpriced"])}
    return result


def _statement(items, cols, valuation, shop):
    gold = next((item for item in items if item["name"] == GOLD), None)
    stock = None if valuation is None else {
        col["key"]: sum(valuation[group]["flows"].get(col["key"], 0) for group in VALUED) for col in cols}
    rows = []
    for col in cols:
        key = col["key"]
        group = "operating" if key in ("daily", "collect", "shop") else "outside"
        cash = gold["deltas"].get(key) if gold else None
        amount = None if stock is None else stock[key]
        if key == "shop" and shop and cash is not None:
            money = shop["gold"]
            rest = "常规采购支出" if money.get("regular", True) else "未分清的金币（余额项）"
            for label, value, basis in (("出售收入", money["sales"], "booked"), ("补买支出", money["replenish"], "booked"),
                                        (rest, money["residual"], "derived")):
                rows.append({"group": group, "col": key, "label": f"商店套利：{label}", "cash": value, "stock": None,
                             "basis": basis, "part": True})
        rows.append({"group": group, "col": key, "label": {"daily": "日常任务", "collect": "地图吸取", "shop": "商店套利",
                                                            "outside": "期间外（手动操作等）", "mixed": "混合（未能分开）"}[key],
                     "cash": cash, "stock": amount, "basis": "counted", "part": False})
    if valuation is not None:
        rows.append({"group": "valuation", "col": None, "label": "公允价值变动（单价调整）", "cash": 0,
                     "stock": sum(valuation[group]["reval"] for group in VALUED), "basis": "derived", "part": False})
    span_cash = sum(span["delta"] for span in gold["spans"]) if gold else 0
    span_stock = None if valuation is None else sum(valuation[group]["spans"] for group in VALUED)
    if span_cash or span_stock:
        rows.append({"group": "outside", "col": None, "label": "跨区间合并", "cash": span_cash, "stock": span_stock,
                     "basis": "counted", "part": False})
    whole = [row for row in rows if not row["part"]]
    cash_known = [row["cash"] for row in whole if row["cash"] is not None]
    totals = {"cash": sum(cash_known) if gold else None,
              "stock": None if valuation is None else sum(row["stock"] or 0 for row in whole)}
    totals["profit"] = None if totals["cash"] is None else totals["cash"] + (totals["stock"] or 0)
    stock_open = None if valuation is None else sum(valuation[group]["opening"] for group in VALUED)
    stock_close = None if valuation is None else sum(valuation[group]["closing"] for group in VALUED)

    def point(cash, amount):
        return {"cash": cash, "stock": amount, "total": None if cash is None else cash + (amount or 0)}

    others = {item["name"]: item["total"] for item in items
              if item["group"] == "money" and item["name"] != GOLD and item["total"]}
    opening = point(gold["opening"] if gold else None, stock_open)
    # 期初调整（U67-1）：上日期末 + 期初调整 = 本日期初；调整不进本日合计。
    cash_adjust = (gold["adjust"] or {}).get("delta", 0) if gold else 0
    stock_adjust = None if valuation is None else sum(valuation[group]["adjust"] for group in VALUED)
    adjustment = carried = None
    if cash_adjust or stock_adjust:
        adjustment = point(cash_adjust if gold else None, stock_adjust)
        carried = point(None if opening["cash"] is None else opening["cash"] - cash_adjust,
                        None if stock_open is None else stock_open - (stock_adjust or 0))
    return {"carried": carried, "adjustment": adjustment, "opening": opening,
            "closing": point(gold["closing"] if gold else None, stock_close),
            "rows": rows, "totals": totals, "other_currencies": others}


def _checks(items, statement, shop, day_intervals, stray, valuation):
    gold = next((item for item in items if item["name"] == GOLD), None)
    broken = [item["name"] for item in items
              if item["opening"] + sum(value for value in item["deltas"].values() if value is not None)
              + sum(span["delta"] for span in item["spans"]) != item["closing"]]
    checks = [{"name": "C1 期初 + Σ各列 + 跨区间合并 = 期末（逐物品）", "ok": not broken,
               "detail": f"{len(items)} 项" if not broken else "不平：" + "、".join(broken[:8])}]
    if gold:
        checks.append({"name": "C2 金币盘点差 = 收益表现金列合计", "ok": gold["total"] == statement["totals"]["cash"],
                       "detail": f"{gold['total']:+,} 对 {statement['totals']['cash']:+,}"})
    if shop and shop["gold"]["net"] is not None:
        money = shop["gold"]
        problems = []
        if money["unknown_sales"]:
            problems.append(f"{'、'.join(money['unknown_sales'])} 的出售没核到金币")
        if money["residual"] > 0:
            problems.append(f"余额项 {money['residual']:+,} 为正，不是常规采购")
        checks.append({"name": "C3 商店余额项只能是常规采购（不为正、出售都核到了金币）", "ok": not problems,
                       "detail": "；".join(problems) if problems else f"余额项 {money['residual']:+,}，即常规采购（推算）"})
    chained = all(left["to"] == right["from"] for left, right in zip(day_intervals, day_intervals[1:]))
    checks.append({"name": "C4 区间首尾相接", "ok": chained, "detail": f"{len(day_intervals)} 段"})
    checks.append({"name": "C5 出售、补买、制作的逐笔记录都落在商店区间里", "ok": not stray,
                   "detail": "是" if not stray else "落在别的区间：" + "、".join(stray)})
    if valuation is not None:
        change = sum(valuation[group]["closing"] - valuation[group]["opening"] for group in VALUED)
        parts = sum(sum(valuation[group]["flows"].values()) + valuation[group]["spans"] + valuation[group]["reval"]
                    for group in VALUED)
        checks.append({"name": "C6 存货估值变动 = Σ 数量变动 × 新单价 + Σ 原有数量 × 单价变动", "ok": change == parts,
                       "detail": f"{change:+,}"})
        closing, opening, profit = statement["closing"]["total"], statement["opening"]["total"], statement["totals"]["profit"]
        if None not in (closing, opening, profit):
            checks.append({"name": "C7 期末合计 = 期初合计 + 损益", "ok": closing == opening + profit,
                           "detail": f"{profit:+,}"})
    return checks


def _notices(status, first_ever, day_intervals, day_scans, tail, checks, shop, valuation, kst, items=()):
    notices = []
    for check in checks:
        if not check["ok"]:
            notices.append({"level": "error", "text": f"自检失败：{check['name']}（{check['detail']}）"})
    for interval in day_intervals:
        span = f"{kst(interval['start'])}–{kst(interval['end'])}"
        label = KIND_LABEL[interval["kind"]]
        if interval["kind"] == "mixed":
            what = "、".join(interval["strangers"]) or "、".join(KIND_LABEL[kind] for kind in interval["types"])
            notices.append({"level": "warn", "text": f"{span} 这一段没能分开（{what}），整段单列「混合」。"
                                                     "通常是中间有一次盘点没执行。"})
        if "abnormal_end" in interval["flags"]:
            notices.append({"level": "warn", "text": f"{span} 的{label}没走到正常结束点，变化仍记在{label}列。"})
        if "unsettled" in interval["flags"]:
            notices.append({"level": "warn", "text": f"{span} 的{label}没有当场结清：收尾的盘点隔了一段才来，"
                                                     "其间的手动操作与它分不开。"})
        if "partial_scan" in interval["flags"]:
            notices.append({"level": "warn", "text": f"{span} 之间有一次盘点盘到一半就断了，没有当作边界。"})
    for position, scan in enumerate(day_scans):
        if scan["unknown"]:
            if position == 0 and not first_ever:
                how = "它们本日的期初取当天第一次读到的数，之前的变化并进期初调整。"
            elif position == len(day_scans) - 1:
                how = ("这是当天最后一盘：它们本日的期末取更早的读数，之后的变化记在下一日的期初调整。" if status == "final"
                       else "这是到目前为止最后一盘：它们的期末先取更早的读数，今天再盘到会补上。")
            else:
                how = "它们前后两段合并显示。"
            notices.append({"level": "warn", "text": f"{kst(scan['at'])} 的盘点有 {len(scan['unknown'])} 项没读准："
                                                     f"{'、'.join(scan['unknown'][:6])}。{how}"})
        elif not scan["complete"]:
            notices.append({"level": "warn", "text": f"{kst(scan['at'])} 的盘点没盘完（{scan['termination']}）。"})
    for item in items:
        for miss in item["suspect"]:
            notices.append({"level": "warn", "text": f"{miss['scan']} 那次盘点把「{item['name']}」判成了 0，但前后两次都是 "
                                                     f"{miss['value']:,}：按漏读处理，没有计入变动。"})
        if item["emptied"]:
            notices.append({"level": "warn", "text": f"「{item['name']}」从 {item['emptied']:,} 变成了 0，这一段没有它的出售或制作记录，"
                                                     "可能是盘点漏读；当天再读到它会自动更正，隔天才读回来的记成那天的期初调整。"})
    adjusted = [item for item in items if item["adjust"]]
    if adjusted:
        # 隔夜那一段的变化是平常事；本日第一盘没读到、或上一张表上是漏读的 0，才值得提醒。
        late = [item["name"] for item in adjusted if item["adjust"]["reason"] != "overnight"]
        names = [item["name"] for item in adjusted]
        notices.append({"level": "warn" if late else "info",
                        "text": f"{len(names)} 项的本日期初与上日期末不同（{'、'.join(names[:6])}" + ("等" if len(names) > 6 else "")
                                + "）：上日最后一次读到它们之后、本日第一次读到之前的变化，差额见「期初调整」表，不计入本日各列与损益。"
                                + (f"其中 {len(late)} 项本日第一盘没读到或是漏读更正（{'、'.join(late[:6])}"
                                   + ("等" if len(late) > 6 else "") + "）。" if late else "")})
    if shop and shop["mismatch"]:
        notices.append({"level": "warn", "text": "商店推算失配：" + "、".join(shop["mismatch"][:8]) + "（见表二）。"})
    if valuation is not None:
        missing = [name for group in VALUED for name in valuation[group]["unpriced"]]
        if missing:
            notices.append({"level": "warn", "text": f"{len(missing)} 项存货没有单价，未计入估值：{'、'.join(missing[:6])}。"})
    pending = "、".join(dict.fromkeys(KIND_LABEL.get(kind, kind) for kind in tail["types"]))
    if status == "interim":
        notices.append({"level": "info", "text": "本日尚未结账：下一个游戏日的第一次盘点到来时结账，结账用的就是到那时为止最后一次记账的数。"
                        + (f"最后一次盘点之后还有没盘掉的活动（{pending}）：今天再盘到才进本日的表，否则记在下一日的期初调整。"
                           if pending else "")})
    elif pending:
        notices.append({"level": "warn", "text": f"当天最后一次盘点之后还跑过{pending}，没有再盘：这部分变化不在本日的表里，"
                                                 "记在下一日的期初调整。"})
    if first_ever:
        notices.append({"level": "info", "text": "这是有记账盘点的第一天：期初就是当天第一次盘点，此前的变化无从算起。"})
    return notices


def build_report(ledger, *, game_day: str, as_of: str, prices=None, cookbook=None, account=None, labels=None,
                 kst=None, history_before=False, prior=None) -> dict | None:
    """账 → 某一个游戏日的报表模型；当天没有任何记账盘点返回 None。

    prior：上一张定稿表的期末余额（它的 balance["items"]）。本日期初与它对不上的差额记成期初调整；
    不给就从账里更早的读数推（没有定稿的表可用时：头一天、离线拿整本账单算某一天）。
    结账定稿的表自己也带 balance，供下一日结转。

    prices：{盘点编号: {物品名: {"unit", "basis", "price_day"}}}，不给就只出数量。
    cookbook：{"recipes": {料理: {材料: 用量}}, "purchasable": 可在商店买到的材料}，用于商店账实核对的推算。
    labels：任务入口 → 界面上的显示名。kst：把 ISO 时刻写成页面上的时刻的函数。
    history_before：账里看不到、但更早的日子其实出过报表（只装载了近期凭证时），用来判断这是不是有盘点的第一天。
    """
    scans = ledger["scans"]
    kst = kst or (lambda text: str(text)[11:16])
    indexes = [index for index, scan in enumerate(scans) if scan["day"] == game_day]
    if not indexes:
        return None
    first, last = indexes[0], indexes[-1]
    # 之后有更晚游戏日的盘点＝已结账。结账用的就是当天最后一盘为止的数，不拿那一盘来改它（U76）：
    # 那一盘是次日自己的期初，它与本日期末的差额记在次日的期初调整。
    final = last + 1 < len(scans)
    day_scans = scans[first:last + 1]
    day_intervals = [dict(interval) for interval in ledger["intervals"][first:last]]
    for interval in day_intervals:
        interval["tasks"] = [{**task, "label": (labels or {}).get(task["entry"], task["entry"])} for task in interval["tasks"]]
    scans_by_id = {scan["id"]: scan for scan in scans}
    cols = _columns(day_intervals, scans_by_id)
    carried_in = _ledger_prior(ledger, first, prices) if prior is None else prior
    chains = items_until(ledger, last)   # 只看到当天最后一盘为止
    items, unread = _items(ledger, chains, first, prices, carried_in)
    for item in items:   # 该列的区间两头都不扫它的科目 → 「不扫」
        item["gaps"] = [col["key"] for col in cols if item["deltas"].get(col["key"]) is None
                        and item["scope"] not in (None, "$") and item["scope"] not in col["scopes"]]
    changed = [item for item in items if item["total"] or item["spans"]
               or any(value for value in item["deltas"].values())]
    # 期初调整表：只列期初与上日期末对不上的；货币在前，其余按科目、调整量大小。
    order = {key: position for position, (key, _) in enumerate(GROUPS)}
    adjustments = [{"name": item["name"], "group": item["group"], "before": item["adjust"]["before"],
                    "before_at": item["adjust"]["before_at"], "before_time": item["adjust"]["before_time"],
                    "price_before": item["adjust"]["price_before"], "delta": item["adjust"]["delta"],
                    "reason": item["adjust"]["reason"], "value": item["adjust"]["value"], "opening": item["opening"],
                    "opening_at": item["opening_at"], "opening_time": scans_by_id[item["opening_at"]]["at"],
                    "price": item["price_open"]}
                   for item in sorted((item for item in items if item["adjust"]),
                                      key=lambda item: (order.get(item["group"], len(order)), item["name"] != GOLD,
                                                        -abs(item["adjust"]["delta"]), item["name"]))]
    names = {item["name"] for item in changed}
    unchanged = [item for item in items if item["name"] not in names]
    valuation = None if prices is None else _valuation(items)
    book, stray = _merge_books(day_intervals)
    has_shop = any(col["key"] == "shop" for col in cols)
    shop = _shop(items, book, cookbook) if has_shop else None
    statement = _statement(items, cols, valuation, shop)
    checks = _checks(items, statement, shop, day_intervals, stray, valuation)
    by_card = _by_card(ledger, chains, first, day_intervals, prices)
    if by_card and by_card["recorded"]:
        checks.append(_by_card_check(items, by_card))
    status = "final" if final else "interim"
    first_ever = first == 0 and not history_before
    # 当天最后一盘之后：期中是账尾（还没盘掉的活动）；已结账的是它到次日第一盘之间那一段，不在本日的表里。
    after = ledger["intervals"][last] if final else None
    tail = {"types": list(after["types"]) + (["mixed"] if after["strangers"] else [])} if after else ledger["tail"]
    notices = _notices(status, first_ever, day_intervals, day_scans, tail, checks, shop, valuation, kst, items)
    # 上日最后一盘到本日第一盘之间那一段（期初调整吞掉的主要就是它）：跑过什么，给期初调整表做说明。
    before = ledger["intervals"][first - 1] if first > 0 else None
    overnight = None if before is None else {
        "from": scans[first - 1]["at"], "to": scans[first]["at"], "minutes": before["minutes"], "kind": before["kind"],
        "tasks": [(labels or {}).get(task["entry"], task["entry"]) for task in before["tasks"]]}
    gold = next((item for item in items if item["name"] == GOLD), None)
    collected = [item for item in items if item["group"] in VALUED and (item["deltas"].get("collect") or 0) > 0]
    kpis = {"gold_delta": gold["total"] if gold else None,
            "shop_cash": gold["deltas"].get("shop") if gold else None,
            "outside_cash": gold["deltas"].get("outside") if gold else None,
            "profit": statement["totals"]["profit"] if valuation is not None else None,
            "stock_delta": statement["totals"]["stock"],
            "collect_items": len(collected),
            "collect_top": [[item["name"], item["deltas"]["collect"]]
                            for item in sorted(collected, key=lambda item: -item["deltas"]["collect"])[:3]],
            "collect_value": None if valuation is None else sum(
                valuation[group]["flows"].get("collect", 0) for group in VALUED),
            "scans": len(day_scans), "scan_seconds": sum(scan["seconds"] for scan in day_scans),
            "warnings": sum(notice["level"] != "info" for notice in notices),
            "adjusted": sum(bool(item["adjust"]) for item in items)}
    money = sorted((item for item in items if item["group"] == "money"),
                   key=lambda item: (item["category"] != "currency", item["name"] != GOLD, item["name"]))
    assets = {"currency": [{"name": item["name"], "opening": item["opening"], "closing": item["closing"],
                            "opening_at": item["opening_at"], "closing_at": item["closing_at"]}
                           for item in money   # 货币都列；票券只列手里有的
                           if item["category"] == "currency" or item["opening"] or item["closing"]],
              "groups": [{"group": key, "label": label, "read": sum(item["group"] == key for item in items),
                          "value": None if valuation is None or key not in VALUED else {
                              "opening": valuation[key]["opening"], "closing": valuation[key]["closing"]}}
                         for key, label in GROUPS if key != "money" and any(item["group"] == key for item in items)],
              "total": {"opening": statement["opening"]["total"], "closing": statement["closing"]["total"],
                        "valued": valuation is not None}}
    # 期末余额（结转）：定稿才有。当天读到过的取它的期末；当天没读到的沿用上一张表的那一行（时刻不变，看得出是沿用）。
    balance = None
    if final:
        rows = {item["name"]: _balance_row(item["closing"], scans_by_id[item["closing_at"]]["at"], item["price"])
                for item in items}
        for name, row in carried_in.items():
            rows.setdefault(name, dict(row))
        balance = {"as_of": scans[last]["at"], "items": dict(sorted(rows.items()))}
    cards = [card for interval in day_intervals for card in interval["cards"] or ()]
    collects = [interval for interval in day_intervals if interval["kind"] == "collect"]
    model = {
        "schema": SCHEMA, "rule_version": ledger["rule_version"], "account": account or {"hash": None, "alias": None},
        "game_day": game_day, "as_of": as_of, "status": status, "first_scan": first_ever,
        "scans": [{key: scan[key] for key in ("id", "scan_id", "seq", "at", "label", "scope", "seconds", "pages", "read",
                                              "absent", "unknown", "complete", "termination")}
                  | {"retried": scan.get("retried", 0)}
                  | {"gold": scan["readings"].get(GOLD)} for scan in day_scans],
        "segments": day_intervals, "cols": cols, "items": changed, "adjustments": adjustments,
        "unchanged": [{"name": item["name"], "group": item["group"], "quantity": item["closing"],
                       "value": item["value"]["closing"] if item["value"] else None} for item in unchanged],
        "unread": unread, "shop": shop, "valuation": valuation, "assets": assets, "statement": statement,
        "balance": balance, "overnight": overnight,
        "collect": {"runs": len(collects), "cards": cards if any(interval["cards"] is not None for interval in collects)
                    else None, "abnormal": sum("abnormal_end" in interval["flags"] for interval in collects),
                    "by_card": by_card},
        "checks": checks, "notices": notices, "kpis": kpis}
    model["summary"] = summarize(model)
    return model


def _signed(value) -> str:
    if value is None:
        return "?"
    return f"({abs(value):,})" if value < 0 else f"+{value:,}"


def summarize(model) -> str:
    """一行摘要：进日志，也写在页脚。"""
    kpis = model["kpis"]
    parts = [("⚠ 自检失败 · " if any(not check["ok"] for check in model["checks"]) else "") + model["game_day"][5:]
             + ("（未结账）" if model["status"] == "interim" else "")]
    if not model["segments"]:
        return " · ".join(parts + ["只有一次盘点，还没有可比较的变动"])
    if kpis["gold_delta"] is not None:
        inner = [f"{label} {_signed(kpis[key])}" for label, key in (("商店", "shop_cash"), ("期间外", "outside_cash"))
                 if kpis[key]]
        parts.append(f"金币 {_signed(kpis['gold_delta'])}" + (f"（{'，'.join(inner)}）" if inner else ""))
    if kpis["profit"] is not None:
        parts.append(f"损益 {_signed(kpis['profit'])}")
    if kpis["collect_items"]:
        top = "，".join(f"{name} +{amount:,}" for name, amount in kpis["collect_top"][:1])
        parts.append(f"吸取 {kpis['collect_items']} 项" + (f"，{top}" if top else ""))
    parts.append(f"盘点 {kpis['scans']} 次 {kpis['scan_seconds'] // 60} 分")
    if kpis.get("adjusted"):
        parts.append(f"期初调整 {kpis['adjusted']} 项")
    if kpis["warnings"]:
        parts.append(f"{kpis['warnings']} 条提醒")
    return " · ".join(parts)
