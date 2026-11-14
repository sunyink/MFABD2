"""库存记账：从账制表——某一个游戏日的报表模型。

纯函数，只吃账（inventory_ledger.build_ledger 的结果），不回头看凭证、不读写文件。
归日：区间归到它开始那次盘点所在的游戏日；一日的期初是当天第一盘，期末是下一个游戏日第一盘的读数，
隔夜那一段记在上一日。下一个游戏日的盘点还没来时只能记账（期中），来了才结账（期末）。
"""

SCHEMA = "ledger-report/2"
KIND_LABEL = {"daily": "日常", "collect": "吸取", "shop": "商店", "outside": "期间外", "mixed": "混合", "later": "次日以后"}
GROUPS = (("money", "货币票券"), ("ingredient", "存货 · 食材"), ("dish", "存货 · 料理"), ("growth", "养成材料"),
          ("other", "其他"))
GROUP_OF = {"currency": "money", "ticket": "money", "ingredient": "ingredient", "dish": "dish", "growth": "growth",
            "craft": "growth"}
VALUED = ("ingredient", "dish")
GOLD = "金币"
REASONS = {"unknown": "中间有盘点没读到它", "unread": "中间有盘点没读到顶栏", "out_of_scope": "中间的盘点不扫这一科目",
           "missed": "中间有盘点没盘完"}


def _unit(prices, scan, name):
    if not prices:
        return None
    row = (prices.get(scan["scan_id"]) or {}).get(name)
    return row if isinstance(row, dict) and type(row.get("unit")) is int else None


def _items(ledger, first, last, close, prices):
    """逐物品：当天的期初、各列变动、跨区间合并、期末，以及（给了单价时）量价分解。"""
    scans, intervals = ledger["scans"], ledger["intervals"]
    rows, unread = [], []
    for name, entry in ledger["items"].items():
        readings = [(index, quantity) for index, quantity in entry["readings"] if first <= index <= last]
        if not readings:
            unread.append(name)
            continue
        pieces = [piece for piece in entry["pieces"] if first <= piece["from"] <= last]
        opening_at, opening = readings[0]
        closing_at, closing = (pieces[-1]["to"], pieces[-1]["close"]) if pieces else readings[-1]
        group = GROUP_OF.get(entry["category"], "other")
        valued = group in VALUED and prices is not None
        price_open = _unit(prices, scans[opening_at], name) if valued else None
        price = _unit(prices, scans[closing_at], name) if valued else None
        deltas, spans, flows, reval, priced = {}, [], {}, 0, valued and price_open is not None and price is not None
        for piece in pieces:
            kinds = list(dict.fromkeys(intervals[index]["kind"] if index < close else "later"
                                       for index in range(piece["from"], piece["to"])))
            before, after = (_unit(prices, scans[piece["from"]], name), _unit(prices, scans[piece["to"]], name)) \
                if valued else (None, None)
            priced = priced and before is not None and after is not None
            flow = piece["delta"] * after["unit"] if priced else None
            if priced:
                reval += piece["open"] * (after["unit"] - before["unit"])
            if len(kinds) == 1 and kinds[0] != "later":
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
                     "spans": sum(span["flow"] for span in spans), "reval": reval}
        rows.append({
            "name": name, "category": entry["category"], "group": group, "kind": entry.get("kind"),
            "scope": entry.get("scope"), "opening": opening, "closing": closing, "total": closing - opening,
            "opening_at": scans[opening_at]["id"], "closing_at": scans[closing_at]["id"],
            "deltas": deltas, "spans": spans, "price": price, "price_open": price_open, "value": value,
            "unpriced": valued and not priced})
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
        rows.append({"name": name, "net": net, "replenish": bought or None, "sales": -sold if sold else None,
                     "cooking": cooking or None, "residual": residual, "mismatch": mismatch,
                     "dish": name in recipes or name in book["cooked"], "basis": "；".join(notes)})
    rows.sort(key=lambda row: (not row["dish"], -abs(row["net"] or 0), row["name"]))
    gold = by_name.get(GOLD)
    net = gold["deltas"].get("shop") if gold else None
    sales = sum(row["gold"] for row in book["sales"].values())
    spent = sum(row["spent"] for row in book["replenish"].values())
    return {"gold": {"net": net, "sales": sales, "replenish": -spent,
                     "residual": None if net is None else net - sales + spent, "unconfirmed_sales": book["unconfirmed"]},
            "rows": rows, "cooked": [{"recipe": dish, **row} for dish, row in sorted(book["cooked"].items())],
            "mismatch": [row["name"] for row in rows if row["mismatch"]]}


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
            for label, value, basis in (("出售收入", money["sales"], "booked"), ("补买支出", money["replenish"], "booked"),
                                        ("常规采购支出", money["residual"], "derived")):
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
    return {"opening": point(gold["opening"] if gold else None, stock_open),
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
        checks.append({"name": "C3 商店金币净额 = 出售 + 补买 + 余额项",
                       "ok": money["net"] == money["sales"] + money["replenish"] + money["residual"],
                       "detail": f"余额项 {money['residual']:+,}，即常规采购（推算）"})
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


def _notices(status, first_ever, day_intervals, day_scans, tail, checks, shop, valuation, kst):
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
    for scan in day_scans:
        if scan["unknown"]:
            notices.append({"level": "warn", "text": f"{kst(scan['at'])} 的盘点有 {len(scan['unknown'])} 项没读准："
                                                     f"{'、'.join(scan['unknown'][:6])}。它们前后两段合并显示。"})
        elif not scan["complete"]:
            notices.append({"level": "warn", "text": f"{kst(scan['at'])} 的盘点没盘完（{scan['termination']}）。"})
    if shop and shop["mismatch"]:
        notices.append({"level": "warn", "text": "商店推算失配：" + "、".join(shop["mismatch"][:8]) + "（见表二）。"})
    if valuation is not None:
        missing = [name for group in VALUED for name in valuation[group]["unpriced"]]
        if missing:
            notices.append({"level": "warn", "text": f"{len(missing)} 项存货没有单价，未计入估值：{'、'.join(missing[:6])}。"})
    if status == "interim":
        pending = "、".join(dict.fromkeys(KIND_LABEL.get(kind, kind) for kind in tail["types"]))
        notices.append({"level": "info", "text": "本日尚未结账：下一个游戏日的第一次盘点到来时结账。"
                        + (f"最后一次盘点之后还有没盘掉的活动（{pending}）。" if pending else "")})
    if first_ever:
        notices.append({"level": "info", "text": "这是有记账盘点的第一天：期初就是当天第一次盘点，此前的变化无从算起。"})
    return notices


def build_report(ledger, *, game_day: str, as_of: str, prices=None, cookbook=None, account=None, labels=None,
                 kst=None, history_before=False) -> dict | None:
    """账 → 某一个游戏日的报表模型；当天没有任何记账盘点返回 None。

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
    final = last + 1 < len(scans)
    close = last + 1 if final else last
    day_scans = scans[first:close + 1]
    day_intervals = [dict(interval) for interval in ledger["intervals"][first:close]]
    for interval in day_intervals:
        interval["tasks"] = [{**task, "label": (labels or {}).get(task["entry"], task["entry"])} for task in interval["tasks"]]
    scans_by_id = {scan["id"]: scan for scan in scans}
    cols = _columns(day_intervals, scans_by_id)
    items, unread = _items(ledger, first, last, close, prices)
    for item in items:   # 该列的区间两头都不扫它的科目 → 「不扫」
        item["gaps"] = [col["key"] for col in cols if item["deltas"].get(col["key"]) is None
                        and item["scope"] not in (None, "$") and item["scope"] not in col["scopes"]]
    changed = [item for item in items if item["total"] or item["spans"]
               or any(value for value in item["deltas"].values())]
    names = {item["name"] for item in changed}
    unchanged = [item for item in items if item["name"] not in names]
    valuation = None if prices is None else _valuation(items)
    book, stray = _merge_books(day_intervals)
    has_shop = any(col["key"] == "shop" for col in cols)
    shop = _shop(items, book, cookbook) if has_shop else None
    statement = _statement(items, cols, valuation, shop)
    checks = _checks(items, statement, shop, day_intervals, stray, valuation)
    status = "final" if final else "interim"
    first_ever = first == 0 and not history_before
    notices = _notices(status, first_ever, day_intervals, day_scans, ledger["tail"] if not final else {"types": []},
                       checks, shop, valuation, kst)
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
            "warnings": sum(notice["level"] != "info" for notice in notices)}
    money = sorted((item for item in items if item["group"] == "money"),
                   key=lambda item: (item["category"] != "currency", item["name"] != GOLD, item["name"]))
    assets = {"currency": [{"name": item["name"], "opening": item["opening"], "closing": item["closing"]}
                           for item in money],
              "groups": [{"group": key, "label": label, "read": sum(item["group"] == key for item in items),
                          "value": None if valuation is None or key not in VALUED else {
                              "opening": valuation[key]["opening"], "closing": valuation[key]["closing"]}}
                         for key, label in GROUPS if key != "money" and any(item["group"] == key for item in items)],
              "total": {"opening": statement["opening"]["total"], "closing": statement["closing"]["total"],
                        "valued": valuation is not None}}
    cards = [card for interval in day_intervals for card in interval["cards"] or ()]
    collects = [interval for interval in day_intervals if interval["kind"] == "collect"]
    model = {
        "schema": SCHEMA, "rule_version": ledger["rule_version"], "account": account or {"hash": None, "alias": None},
        "game_day": game_day, "as_of": as_of, "status": status, "first_scan": first_ever,
        "scans": [{key: scan[key] for key in ("id", "scan_id", "seq", "at", "label", "scope", "seconds", "pages", "read",
                                              "absent", "unknown", "complete", "termination")}
                  | {"gold": scan["readings"].get(GOLD)} for scan in day_scans],
        "segments": day_intervals, "cols": cols, "items": changed,
        "unchanged": [{"name": item["name"], "group": item["group"], "quantity": item["closing"],
                       "value": item["value"]["closing"] if item["value"] else None} for item in unchanged],
        "unread": unread, "shop": shop, "valuation": valuation, "assets": assets, "statement": statement,
        "collect": {"runs": len(collects), "cards": cards if any(interval["cards"] is not None for interval in collects)
                    else None, "abnormal": sum("abnormal_end" in interval["flags"] for interval in collects)},
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
    if kpis["warnings"]:
        parts.append(f"{kpis['warnings']} 条提醒")
    return " · ".join(parts)
