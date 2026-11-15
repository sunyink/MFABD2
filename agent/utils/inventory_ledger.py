"""库存记账：从凭证做账。

纯函数：同样的凭证永远得到同样的账，不读写文件、不依赖框架。运行时只留凭证（盘点读数、标记、货币读数、
商店逐笔记录），段怎么划、算谁的都在这里现算；规则改了重算即可，不迁移数据。

账的组成：
    scans      记账盘点（带 origin 的 inventory_scan），按时刻排；每次盘点是一个时点
    intervals  相邻两次记账盘点之间的一段：归属（daily / collect / shop / outside / mixed）、标志、跑过的任务、逐笔记录
    items      每样东西自己的读数链：相邻两次「读到它」的盘点之间是一段变化
    tail       最后一次盘点之后还没盘掉的活动
"""

from datetime import datetime, timezone

from .ledger_rules import GAP_SECONDS, _connected, game_day

RULE_VERSION = 2
# 顶栏货币在账里的名字 → 货币读数里的字段。
CURRENCY = (("金币", "gold"), ("钻石", "diamond"), ("钻石（免费）", "diamond_free"),
            ("钻石（付费）", "diamond_paid"), ("天赋神药", "tonic"))
CURRENCY_SCOPE = "$"
# 周期完成记录里属于采集卡带的键名前缀（剧情、活动、角色卡带）；别的是日常任务之类的完成记录。
CARD_PREFIXES = ("Story_", "Event_", "Character_")
# 判成 0 的读数夹在两次相同的读数之间、数量不小于它，就当作那次盘点漏读（做账参数，改了重算）。
# 定得偏大是因为有上限的小额物品（用光、次日补满）会合法地出现「有 → 0 → 同一个数」。
SUSPECT_MIN = 100
_SALE, _BUY, _UNSURE = "sale_confirmed", "replenish_purchase", "sale_unconfirmed"


def ts(text) -> float | None:
    """ISO 8601 时刻 → 秒；没有时区的、解析不了的返回 None。"""
    try:
        value = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return value.timestamp() if value.tzinfo else None


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def dedupe(vouchers) -> list:
    """热档与冷档会重叠：有事件编号的按编号去重，没有的按各类凭证自己的键。"""
    seen, result = set(), []
    for event in vouchers:
        if not isinstance(event, dict):
            continue
        kind, reference = event.get("kind"), event.get("reference") or {}
        if event.get("event_id"):
            key = ("id", event["event_id"])
        elif kind == "inventory_scan":
            key = (kind, event.get("scan_id"))
        elif kind == "ledger_mark":
            key = (kind, event.get("session"), (event.get("task") or {}).get("task_id"), event.get("node"),
                   event.get("mark"), event.get("at"))
        elif kind == "currency_reading":
            key = (kind, event.get("scan_id"), event.get("observed_at"))
        elif kind == "cooking_made":
            key = (kind, event.get("recipe"), event.get("started_at"))
        else:
            key = (kind, event.get("observed_at"), event.get("reason"), reference.get("scan_id"), reference.get("page"),
                   tuple(sorted(event.get("quantities") or event.get("items") or ())))
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


def _scan_rows(events, currencies) -> list:
    rows = []
    for event in events:
        at = ts(event.get("finished_at"))
        if event.get("kind") != "inventory_scan" or not isinstance(event.get("origin"), dict) or at is None:
            continue
        readings = {name: quantity for name, quantity in (event.get("quantities") or {}).items()
                    if type(quantity) is int and quantity >= 0}
        readings.update(dict.fromkeys(event.get("absent") or (), 0))
        currency = currencies.get(event.get("scan_id")) or {}
        for name, field in CURRENCY:
            if type(currency.get(field)) is int:
                readings[name] = currency[field]
        started = ts(event.get("started_at")) or at
        rows.append({
            "scan_id": event.get("scan_id"), "seq": event.get("seq"), "at": at, "started": min(started, at),
            "day": game_day(at), "label": event.get("label"), "origin": event["origin"],
            "scope": list(event.get("scope") or ()), "complete": bool(event.get("complete")),
            "termination": event.get("termination"), "pages": event.get("pages") or 0,
            "seconds": round(at - min(started, at)), "read": len(event.get("quantities") or {}),
            "absent": len(event.get("absent") or ()), "unknown": sorted(event.get("unknown") or ()),
            "currency_read": bool(currency), "readings": readings})
    rows.sort(key=lambda row: (row["at"], row["seq"] or 0))
    for number, row in enumerate(rows, 1):
        row["id"] = f"S{number}"
    return rows


def _origin_key(origin: dict):
    return origin.get("session"), (origin.get("task") or {}).get("task_id"), origin.get("node"), origin.get("mark")


def _mark_rows(events, scans) -> list:
    marks = []
    for event in events:
        at = ts(event.get("at"))
        if event.get("kind") != "ledger_mark" or at is None:
            continue
        task = event.get("task") or {}
        marks.append({"mark": event.get("mark"), "type": event.get("type"), "at": at, "session": event.get("session"),
                      "task_id": task.get("task_id"), "entry": task.get("entry") or "", "node": event.get("node"),
                      "cards": event.get("cards"), "tasks": event.get("tasks") or [], "scan": None})
    marks.sort(key=lambda row: row["at"])
    # 这个位置盘了的话，盘点记录的 origin 指回同一个会话、任务、节点；取盘点开始前最近的那条标记。
    for index, scan in enumerate(scans):
        key = _origin_key(scan["origin"])
        owner = None
        for mark in marks:
            if mark["at"] > scan["started"] + 1:
                break
            if (mark["session"], mark["task_id"], mark["node"], mark["mark"]) == key:
                owner = mark
        if owner is not None:
            owner["scan"] = index
    return marks


def _task_rows(marks, scans) -> dict:
    """把各条标记带的任务起止按（会话，任务号）合并，再算每个任务真正动库存的时段。"""
    tasks = {}
    for mark in marks:
        rows = list(mark["tasks"])
        rows.append({"entry": mark["entry"], "task_id": mark["task_id"], "start": None, "end": None, "status": None})
        for row in rows:
            key = (mark["session"], row.get("task_id"))
            task = tasks.setdefault(key, {"session": mark["session"], "task_id": row.get("task_id"),
                                          "entry": row.get("entry") or "", "start": None, "end": None,
                                          "status": None, "marks": []})
            start, end = ts(row.get("start")), ts(row.get("end"))
            if start is not None and (task["start"] is None or start < task["start"]):
                task["start"] = start
            if end is not None and (task["end"] is None or end >= task["end"]):
                task["end"], task["status"] = end, row.get("status")
            task["entry"] = task["entry"] or row.get("entry") or ""
        tasks[(mark["session"], mark["task_id"])]["marks"].append(mark)
    for task in tasks.values():
        own = task["marks"]
        first = min((mark["at"] for mark in own), default=None)
        last = max((mark["at"] for mark in own), default=None)
        begin = task["start"] if task["start"] is not None else first
        finish = task["end"] if task["end"] is not None else last
        # 始标记触发了盘点：活动从那次盘点之后算起。别的位置触发了盘点：活动到那次盘点开始为止。
        opened = [scans[mark["scan"]]["at"] for mark in own if mark["mark"] == "begin" and mark["scan"] is not None]
        closed = [scans[mark["scan"]]["started"] for mark in own if mark["mark"] != "begin" and mark["scan"] is not None]
        if opened:
            begin = min(opened)
        if closed:
            finish = max(closed)
        task["from"] = begin
        task["to"] = begin if finish is None or begin is None else max(finish, begin)
    return {key: task for key, task in tasks.items() if task["from"] is not None}


def _book(events, lower, upper) -> dict:
    """区间里逐笔记下的事实：出售、补买、料理制作。"""
    book = {"sales": {}, "replenish": {}, "cooked": {}, "unconfirmed": 0, "first": None, "last": None}
    for event in events:
        at = ts(event.get("observed_at"))
        kind, reason, reference = event.get("kind"), event.get("reason"), event.get("reference") or {}
        if at is None or not lower < at <= upper:
            continue
        if kind == "inventory_set" and reason == _SALE:
            gold = [proof["gold"].get("delta") for proof in reference.get("proofs") or ()
                    if isinstance(proof, dict) and isinstance(proof.get("gold"), dict)]
            for name in event.get("quantities") or ():
                row = book["sales"].setdefault(name, {"quantity": 0, "gold": 0})
                row["quantity"] += reference.get("actual_quantity") or 0
                row["gold"] += sum(value for value in gold if type(value) is int)
        elif kind == "inventory_set" and reason == _BUY:
            for name in event.get("quantities") or ():
                row = book["replenish"].setdefault(name, {"quantity": 0, "spent": 0})
                row["quantity"] += reference.get("actual_quantity") or 0
                row["spent"] += reference.get("actual_spent") or 0
        elif kind == "inventory_invalidate" and reason == _UNSURE:
            book["unconfirmed"] += 1
        elif kind == "cooking_made" and event.get("recipe"):
            row = book["cooked"].setdefault(event["recipe"], {"made": 0, "inconsistent": 0})
            if event.get("consistent") and type(event.get("made")) is int:
                row["made"] += event["made"]
            else:
                row["inconsistent"] += 1
        else:
            continue
        book["first"] = at if book["first"] is None else min(book["first"], at)
        book["last"] = at if book["last"] is None else max(book["last"], at)
    return book


def _settled(interval_tasks, closing, tasks, gap) -> bool:
    """收尾那次盘点与区间里最后一个任务是否连续：同一个会话，任务一个接一个、中间没有超过阈值的空档。"""
    if not interval_tasks:
        return True
    last = max(interval_tasks, key=lambda task: task["from"])
    session, closer = closing["origin"].get("session"), (closing["origin"].get("task") or {}).get("task_id")
    if last["session"] != session:
        return False
    rows = sorted((task for task in tasks.values() if task["session"] == session and task["start"] is not None),
                  key=lambda task: task["start"])
    return _connected(rows, last["task_id"], closer, gap)


def _intervals(scans, marks, tasks, events, quiet, gap):
    quiet = frozenset(quiet)
    orphans = _orphan_pages(events, scans)
    intervals = []
    tail = {"marks": [], "tasks": [], "types": []}

    def slot(mark):
        """标记落进哪个区间：始标记触发了盘点 → 以那次盘点开头的区间；其余按时刻。"""
        if mark["mark"] == "begin" and mark["scan"] is not None:
            return mark["scan"]
        for index in range(len(scans) - 1, -1, -1):
            if mark["at"] > scans[index]["at"]:
                return index
        return None

    slots = {}
    for mark in marks:
        index = slot(mark)
        if index is not None:
            slots.setdefault(index, []).append(mark)

    for index in range(len(scans)):
        opening = scans[index]
        closing = scans[index + 1] if index + 1 < len(scans) else None
        upper = closing["at"] if closing else float("inf")
        inside = slots.get(index, [])
        begins = [mark for mark in inside if mark["mark"] == "begin"]
        active = {(mark["session"], mark["task_id"]) for mark in begins}
        overlapping = [task for task in tasks.values()
                       if (task["session"], task["task_id"]) in active
                       or (task["from"] < upper and task["to"] > opening["at"])
                       or (task["from"] == task["to"] and opening["at"] < task["from"] < upper)]   # 只知道一个时刻的
        strangers = [task for task in overlapping if not task["marks"] and task["entry"] not in quiet]
        shown = sorted((task for task in overlapping
                        if (task["session"], task["task_id"]) in active or task["entry"] not in quiet),
                       key=lambda task: task["from"])
        types = list(dict.fromkeys(mark["type"] for mark in begins))
        brief = [{"entry": task["entry"], "task_id": task["task_id"], "session": task["session"],
                  "start": None if task["start"] is None else iso(task["start"]),
                  "end": None if task["end"] is None else iso(task["end"]), "status": task["status"]} for task in shown]
        if closing is None:
            tail = {"marks": [{"mark": mark["mark"], "type": mark["type"], "at": iso(mark["at"]), "entry": mark["entry"]}
                              for mark in inside], "tasks": brief, "types": types}
            break
        if len(types) == 1 and not strangers:
            kind = types[0]
        elif not types and not strangers:
            kind = "outside"
        else:
            kind = "mixed"
        flags = []
        if kind in ("collect", "shop"):
            ended = {(mark["session"], mark["task_id"]) for mark in marks if mark["mark"] == "end" and mark["type"] == kind}
            if any((mark["session"], mark["task_id"]) not in ended for mark in begins):
                flags.append("abnormal_end")
        if kind in ("daily", "collect", "shop") and not _settled(shown, closing, tasks, gap):
            flags.append("unsettled")
        if opening["day"] != closing["day"]:
            flags.append("cross_day")
        if any(opening["at"] < at <= closing["at"] for at in orphans):
            flags.append("partial_scan")
        cards = _cards(inside, kind)
        sessions = {mark["session"] for mark in inside} | {task["session"] for task in shown}
        intervals.append({
            "id": f"I{index + 1}", "from": opening["id"], "to": closing["id"], "start": iso(opening["at"]),
            "end": iso(closing["at"]), "minutes": round((closing["at"] - opening["at"]) / 60), "day": opening["day"],
            "kind": kind, "types": types, "flags": flags, "tasks": brief,
            "strangers": [task["entry"] for task in strangers], "cards": cards, "sessions": len(sessions),
            "closed_by": closing["origin"].get("mark"), "book": _book(events, opening["at"], closing["at"])})
    return intervals, tail


def _orphan_pages(events, scans) -> list:
    """只有逐页读数、没有盘点记录的那次（盘到一半进程没了）：它不是边界，只在所在区间上留个标志。"""
    known = {scan["scan_id"] for scan in scans} | {
        event.get("scan_id") for event in events if event.get("kind") == "inventory_scan"}
    result = {}
    for event in events:
        reference = event.get("reference") or {}
        if (event.get("kind") == "inventory_set" and event.get("reason") == "bag_detail"
                and reference.get("scan_id") not in known and str(reference.get("label") or "").startswith("记账")):
            at = ts(event.get("observed_at"))
            if at is not None:
                result[reference.get("scan_id")] = at
    return list(result.values())


def _cards(marks, kind):
    """吸取区间吸完的卡带：区间里最后一份周期记录快照对第一份的差。没走到正常结束点就只有一份，返回 None。"""
    if kind != "collect":
        return None
    snapshots = [mark["cards"] for mark in marks if mark["type"] == "collect" and isinstance(mark["cards"], dict)]
    if len(snapshots) < 2:
        return None
    first, last = snapshots[0], snapshots[-1]   # {"卡带名@周期": 完成时刻}：时刻变了的就是这一段里吸完的
    return sorted(key.split("@")[0] for key, stamp in last.items()
                  if first.get(key) != stamp and key.startswith(CARD_PREFIXES))


def _drop_suspect_zeros(name, readings, scans, intervals):
    """判成 0 的读数夹在两次相同的读数之间，其间又没有它的出售、补买或制作记录：那次盘点漏读了它，这个 0 不作数。"""
    kept, dropped = [], []
    for position, (index, quantity) in enumerate(readings):
        if quantity == 0 and 0 < position < len(readings) - 1:
            (before, held), (after, again) = readings[position - 1], readings[position + 1]
            booked = any(name in intervals[step]["book"][section] for step in range(before, min(after, len(intervals)))
                         for section in ("sales", "replenish", "cooked"))
            if held == again >= SUSPECT_MIN and not booked:
                dropped.append({"scan": scans[index]["id"], "value": held})
                continue
        kept.append((index, quantity))
    return kept, dropped


def _chains(scans, catalog, intervals) -> dict:
    items = {}
    for index, scan in enumerate(scans):
        for name, quantity in scan["readings"].items():
            items.setdefault(name, []).append((index, quantity))
    currency = {name for name, _ in CURRENCY}
    result = {}
    for name, readings in items.items():
        entry = catalog.get(name) or {}
        scope = CURRENCY_SCOPE if name in currency else entry.get("scope")
        readings, suspect = _drop_suspect_zeros(name, readings, scans, intervals)
        doubted = {miss["scan"] for miss in suspect}
        pieces = []
        for (start, opening), (end, closing) in zip(readings, readings[1:]):
            piece = {"from": start, "to": end, "open": opening, "close": closing, "delta": closing - opening}
            if end - start > 1:
                gaps = scans[start + 1:end]
                piece["reason"] = ("suspect_zero" if any(scan["id"] in doubted for scan in gaps)
                                   else "unknown" if any(name in scan["unknown"] for scan in gaps)
                                   else "unread" if scope == CURRENCY_SCOPE
                                   else "out_of_scope" if all(scope not in scan["scope"] for scan in gaps)
                                   else "missed")
                piece["missing_at"] = [scan["id"] for scan in gaps]
            pieces.append(piece)
        result[name] = {"category": "currency" if name in currency else entry.get("category", "other"),
                        "kind": entry.get("kind"), "scope": scope, "suspect": suspect,
                        "readings": [[index, quantity] for index, quantity in readings], "pieces": pieces}
    return result


def build_ledger(vouchers, *, catalog=None, quiet=(), gap: float = GAP_SECONDS) -> dict:
    """凭证 → 账。

    catalog：物品名 → {"category", "scope", "kind"}，决定科目与「这次盘点扫不扫它」；没列的物品照样进账，科目记 other。
    quiet：没留标记也不算活动的任务入口（与运行时同一份名单）。gap：判连续的阈值，秒。
    """
    events = dedupe(vouchers)
    currencies = {}
    for event in events:
        if event.get("kind") == "currency_reading" and event.get("scan_id"):
            currencies[event["scan_id"]] = event   # 同一次盘点读了多回，取后写入的
    scans = _scan_rows(events, currencies)
    marks = _mark_rows(events, scans)
    tasks = _task_rows(marks, scans)
    intervals, tail = _intervals(scans, marks, tasks, events, quiet, gap)
    public = [{key: value for key, value in scan.items() if key != "started"} | {
        "at": iso(scan["at"]), "started_at": iso(scan["started"]), "ts": scan["at"]} for scan in scans]
    return {"rule_version": RULE_VERSION, "gap_seconds": gap, "scans": public, "intervals": intervals,
            "items": _chains(scans, catalog or {}, intervals), "tail": tail}
