"""库存记账：从凭证做账。

纯函数：同样的凭证永远得到同样的账，不读写文件、不依赖框架。运行时只留凭证（盘点读数、标记、货币读数、
商店逐笔记录），段怎么划、算谁的都在这里现算；规则改了重算即可，不迁移数据。

账的组成：
    scans      记账盘点（带 origin 的 inventory_scan），按时刻排；每次盘点是一个时点
    intervals  相邻两次记账盘点之间的一段：归属（daily / collect / shop / exchange / outside / mixed）、标志、跑过的任务、逐笔记录、
               落在这一段的各次进卡读到的掉落（loot，凭证 collect_loot，按第一个采集点的时刻归段）；
               有商店兑换的段另带 exchange（兑换前后两次顶栏读数之差，同一段里兑换与套利按类别拆时用）
    items      每样东西自己的读数链：相邻两次「读到它」的盘点之间是一段变化
    tail       最后一次盘点之后还没盘掉的活动
    token_visits  主页面商店每进一次店一行：店名、进店与离店的代币余额（凭证 token_balance）
"""

from datetime import datetime, timezone

from .ledger_rules import GAP_SECONDS, _connected, game_day

RULE_VERSION = 8   # 3：吸取分卡产出；4：存货没盘全时退出重扫，重扫的读数并进原来那次盘点；5：回读库存确认的出售按报价记金币，取不到记未知
                   # 6：跨过开始那天结账盘点的一笔变化，记成补上读数那天的期初调整（inventory_report，U67-1）
                   # 7：一日的账只算到当天最后一盘，定稿等于最后一次记账；定稿的表带期末余额，下一日第一盘与它的差额
                   #    全部记成期初调整，隔夜那一段不再归上一日（inventory_report，U75、U76）
                   # 8：商店兑换（代币商店）与商店套利同一阶段：同一段里两者都有时按类别拆，B 类归兑换、其余归套利，
                   #    金币与天赋神药按兑换前后两次顶栏读数拆；B 类跨过吸取、套利段的一笔整笔归兑换（代币商店接入计划 §2）
# 顶栏货币在账里的名字 → 货币读数里的字段。
CURRENCY = (("金币", "gold"), ("钻石", "diamond"), ("钻石（免费）", "diamond_free"),
            ("钻石（付费）", "diamond_paid"), ("天赋神药", "tonic"))
CURRENCY_SCOPE = "$"
# 商店兑换前后各读一次的顶栏：这两样按读数之差从同一段里拆给兑换（代币商店接入计划 §2.2）。
EXCHANGE_BAR = (("金币", "gold"), ("天赋神药", "tonic"))
EXCHANGE = "exchange"
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
        elif kind == "collect_loot":
            key = (kind, event.get("session"), (event.get("task") or {}).get("task_id"), event.get("first_at"),
                   event.get("card"))
        elif kind == "token_balance":
            key = (kind, event.get("session"), (event.get("task") or {}).get("task_id"), event.get("shop"),
                   event.get("phase"), event.get("at"))
        else:
            key = (kind, event.get("observed_at"), event.get("reason"), reference.get("scan_id"), reference.get("page"),
                   tuple(sorted(event.get("quantities") or event.get("items") or ())))
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


def _counted(event) -> dict:
    """一条盘点记录读到的数（判 0 的记 0）。"""
    readings = {name: quantity for name, quantity in (event.get("quantities") or {}).items()
                if type(quantity) is int and quantity >= 0}
    readings.update(dict.fromkeys(event.get("absent") or (), 0))
    return readings


def _duration(event) -> float:
    at, started = ts(event.get("finished_at")), ts(event.get("started_at"))
    return 0.0 if at is None or started is None else max(at - started, 0.0)


def _scan_rows(events, currencies) -> list:
    """记账盘点，按时刻排。存货没盘全时同一位置会退出背包重扫（origin.retry_of 指回原来那次）：
    重扫补上的读数并进原来那次，它不另算一次盘点——两次之间什么也没干，分成两个边界只会让补上的东西跨进一个空区间。"""
    scans = [event for event in events if event.get("kind") == "inventory_scan" and isinstance(event.get("origin"), dict)
             and ts(event.get("finished_at")) is not None]
    parents = {event.get("scan_id") for event in scans if not event["origin"].get("retry_of")}
    retries = {}
    for event in scans:
        if event["origin"].get("retry_of") in parents:
            retries.setdefault(event["origin"]["retry_of"], []).append(event)
    rows = []
    for event in scans:
        if event["origin"].get("retry_of") in parents:
            continue
        at = ts(event.get("finished_at")) or 0.0   # 上面已滤掉没有时刻的
        readings, unknown = _counted(event), set(event.get("unknown") or ())
        read, absent = len(event.get("quantities") or {}), len(event.get("absent") or ())
        pages, extra = event.get("pages") or 0, 0.0
        again = sorted(retries.get(event.get("scan_id"), ()), key=lambda retry: ts(retry["finished_at"]) or 0.0)
        for retry in again:
            filled = {name: quantity for name, quantity in _counted(retry).items() if name not in readings}
            readings.update(filled)
            unknown -= set(filled)
            read += sum(quantity > 0 for quantity in filled.values())
            absent += sum(quantity == 0 for quantity in filled.values())
            pages += retry.get("pages") or 0
            extra += _duration(retry)
        currency = currencies.get(event.get("scan_id")) or {}
        for name, field in CURRENCY:
            if type(currency.get(field)) is int:
                readings[name] = currency[field]
        started = ts(event.get("started_at")) or at
        rows.append({
            "scan_id": event.get("scan_id"), "seq": event.get("seq"), "at": at, "started": min(started, at),
            "day": game_day(at), "label": event.get("label"), "origin": event["origin"],
            "scope": list(event.get("scope") or ()),
            "complete": bool(event.get("complete")) or (bool(again) and not unknown),
            "termination": event.get("termination"), "pages": pages,
            "seconds": round(at - min(started, at) + extra), "read": read, "absent": absent,
            "unknown": sorted(unknown), "retried": len(again), "currency_read": bool(currency), "readings": readings})
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


def _loot_rows(events) -> list:
    """吸取掉落凭证：每进一次卡一条，记的是左侧获得列表读到的条目，不是库存读数。"""
    rows = []
    for event in events:
        first, last = ts(event.get("first_at")), ts(event.get("last_at"))
        if event.get("kind") != "collect_loot" or first is None:
            continue
        items = {name: count for name, count in (event.get("items") or {}).items()
                 if isinstance(name, str) and type(count) is int and count > 0}
        rows.append({"card": event.get("card") if isinstance(event.get("card"), str) else None,
                     "end": event.get("end"), "first": first, "last": last if last is not None else first,
                     "points": event.get("points") or 0, "items": items, "unread": event.get("unread") or 0,
                     "stuck": event.get("stuck") or 0, "unknown": sum((event.get("unknown") or {}).values()),
                     "session": event.get("session"), "task_id": (event.get("task") or {}).get("task_id")})
    rows.sort(key=lambda row: row["first"])
    return rows


def _exchange_bars(events) -> dict:
    """商店兑换前后的两次顶栏读数（不跟盘点、带 origin 的 currency_reading）：(会话, 任务号) → {"open", "close"}，
    各是 {账里的货币名: 数}，没读到的那一样不在里面。进店前那次是 probe 位置读的，离店后那次是事后的始标记读的。"""
    bars = {}
    for event in events:
        origin = event.get("origin")
        if event.get("kind") != "currency_reading" or event.get("scan_id") or not isinstance(origin, dict) \
                or origin.get("type") != EXCHANGE or origin.get("mark") not in ("probe", "begin"):
            continue
        side = "open" if origin["mark"] == "probe" else "close"
        values = {name: event[field] for name, field in EXCHANGE_BAR if type(event.get(field)) is int}
        bars.setdefault((origin.get("session"), (origin.get("task") or {}).get("task_id")), {})[side] = values
    return bars


def _token_visits(events) -> list:
    """主页面商店的代币余额：进店一条、离店一条（凭证 token_balance），按会话、任务、店配成一次进店。
    只有进店没有离店的（任务中途停了）照列，离店为空。"""
    rows = sorted((event for event in events if event.get("kind") == "token_balance" and ts(event.get("at")) is not None),
                  key=lambda event: ts(event["at"]) or 0.0)
    visits, open_ = [], {}
    for event in rows:
        key = (event.get("session"), (event.get("task") or {}).get("task_id"), event.get("shop"))
        balance = event.get("balance") if type(event.get("balance")) is int else None
        if event.get("phase") == "enter":
            visit = {"shop": event.get("shop"), "entered": balance, "entered_at": event["at"], "left": None, "left_at": None,
                     "session": key[0], "task_id": key[1]}
            visits.append(visit)
            open_[key] = visit
        elif event.get("phase") == "leave" and key in open_:
            visit = open_.pop(key)
            visit["left"], visit["left_at"] = balance, event["at"]
    for visit in visits:
        both = visit["entered"] is not None and visit["left"] is not None
        visit["spent"] = visit["entered"] - visit["left"] if both else None
    return visits


def _anchored(opening, marks, tasks, gap) -> bool:
    """商店兑换的始标记是事后留的，标记处不盘：这一段开头那次盘点得紧挨着代币商店——同一个会话，从盘点所在的任务到
    代币商店一个接一个、没有超过阈值的空档（进店前补盘的话，那次盘点就在代币商店自己的任务里）。否则兑换前的手动操作
    混进了这一段，数量上与兑换分不开。"""
    session, opener = opening["origin"].get("session"), (opening["origin"].get("task") or {}).get("task_id")
    for mark in marks:
        if mark["session"] != session:
            return False
        rows = sorted((task for task in tasks.values() if task["session"] == session and task["start"] is not None),
                      key=lambda task: task["start"])
        if not _connected(rows, opener, mark["task_id"], gap):
            return False
    return True


def _exchange(begins, kind, opening, tasks, bars, gap):
    """有商店兑换的段：哪几个任务、开头接没接上、同一段里与套利一起时金币与天赋神药各拆多少给兑换。"""
    marks = [mark for mark in begins if mark["type"] == EXCHANGE]
    if not marks:
        return None
    split, unsplit = {}, []
    for name, _ in EXCHANGE_BAR:
        pairs = [bars.get((mark["session"], mark["task_id"])) or {} for mark in marks]
        values = [(pair.get("open") or {}).get(name) for pair in pairs] + [(pair.get("close") or {}).get(name) for pair in pairs]
        if any(value is None for value in values):
            unsplit.append(name)
        else:
            split[name] = sum((pair["close"][name] - pair["open"][name]) for pair in pairs)
    return {"tasks": [mark["task_id"] for mark in marks], "combined": kind != EXCHANGE,
            "anchored": _anchored(opening, marks, tasks, gap), "split": split, "unsplit": unsplit}


def _visits_between(visits, lower, upper) -> list:
    return [{key: iso(value) if key in ("first", "last") else value for key, value in visit.items()}
            for visit in visits if lower < visit["first"] <= upper]


def _sale_gold(proofs):
    """一笔出售凭证的金币：(核对过的金币差, 按报价带上的, 金币未知的几段)。

    quoted_gold：成交时金币差与报价核对过。inventory_delta：金币没核上、回读库存确认成交——
    库存正好少了这一批的选量，就是按开窗时核对过的整行报价成交（用户 11-16：去卖说明当天就是这个价，直接带）；
    少的不是选量那么多、或凭证里没有报价（11-16 之前的凭证），就老老实实记金币未知，不拿 0 充数。
    """
    measured = carried = unknown = 0
    for proof in proofs:
        if not isinstance(proof, dict) or not proof.get("quantity", 1):
            continue
        gold = proof.get("gold")
        if isinstance(gold, dict) and type(gold.get("delta")) is int:
            measured += gold["delta"]
        elif (proof.get("basis") == "inventory_delta" and type(proof.get("quoted_total")) is int
              and proof.get("quantity") == proof.get("selected")):
            carried += proof["quoted_total"]
        else:
            unknown += 1
    return measured, carried, unknown


def _book(events, lower, upper) -> dict:
    """区间里逐笔记下的事实：出售、补买、料理制作。"""
    book = {"sales": {}, "replenish": {}, "cooked": {}, "unconfirmed": 0, "first": None, "last": None}
    for event in events:
        at = ts(event.get("observed_at"))
        kind, reason, reference = event.get("kind"), event.get("reason"), event.get("reference") or {}
        if at is None or not lower < at <= upper:
            continue
        if kind == "inventory_set" and reason == _SALE:
            measured, carried, unknown = _sale_gold(reference.get("proofs") or ())
            for name in event.get("quantities") or ():
                row = book["sales"].setdefault(name, {"quantity": 0, "gold": 0, "carried": 0, "unknown": 0})
                row["quantity"] += reference.get("actual_quantity") or 0
                row["gold"] += measured + carried
                row["carried"] += carried
                row["unknown"] += unknown
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


def _intervals(scans, marks, tasks, events, quiet, gap, visits=(), bars=None):
    quiet = frozenset(quiet)
    orphans = _orphan_pages(events, scans)
    intervals = []
    tail = {"marks": [], "tasks": [], "types": [], "loot": []}

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
                              for mark in inside], "tasks": brief, "types": types,
                    "loot": _visits_between(visits, opening["at"], float("inf"))}
            break
        if len(types) == 1 and not strangers:
            kind = types[0]
        elif set(types) == {"shop", EXCHANGE} and not strangers:
            kind = "shop"   # 同一个「商店」阶段：整段归商店，做账时按类别把 B 类拆给兑换（inventory_report._owner）
        elif not types and not strangers:
            kind = "outside"
        else:
            kind = "mixed"
        flags = []
        if kind in ("collect", "shop"):
            ended = {(mark["session"], mark["task_id"]) for mark in marks if mark["mark"] == "end" and mark["type"] == kind}
            # 只看本类型的始标记：商店兑换的始标记是事后留的，本来就没有结束标记。
            if any((mark["session"], mark["task_id"]) not in ended for mark in begins if mark["type"] == kind):
                flags.append("abnormal_end")
        if kind in ("daily", "collect", "shop", EXCHANGE) and not _settled(shown, closing, tasks, gap):
            flags.append("unsettled")
        exchange = _exchange(begins, kind, opening, tasks, bars or {}, gap) if kind in ("shop", EXCHANGE) else None
        if exchange and not exchange["anchored"]:
            flags.append("unanchored")
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
            "closed_by": closing["origin"].get("mark"), "book": _book(events, opening["at"], closing["at"]),
            "loot": _visits_between(visits, opening["at"], closing["at"]), "exchange": exchange})
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


def items_until(ledger, last: int) -> dict:
    """只看到第 last 次盘点（下标）为止的物品读数链：一日的表只算到当天最后一盘，之后的读数不拿来改它（U76）。

    后来的读数会让「夹在两次相同读数之间的 0」被当成漏读丢掉；截到当天最后一盘，那个 0 在这一天的表上照旧算数，
    读回原数的差额由次日的期初调整接住。
    """
    catalog = {name: {"category": entry["category"], "kind": entry.get("kind"), "scope": entry.get("scope")}
               for name, entry in ledger["items"].items()}
    return _chains(ledger["scans"][:last + 1], catalog, ledger["intervals"][:last])


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
    intervals, tail = _intervals(scans, marks, tasks, events, quiet, gap, _loot_rows(events), _exchange_bars(events))
    public = [{key: value for key, value in scan.items() if key != "started"} | {
        "at": iso(scan["at"]), "started_at": iso(scan["started"]), "ts": scan["at"]} for scan in scans]
    return {"rule_version": RULE_VERSION, "gap_seconds": gap, "scans": public, "intervals": intervals,
            "items": _chains(scans, catalog or {}, intervals), "tail": tail, "token_visits": _token_visits(events)}
