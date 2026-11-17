"""吸取掉落按卡带落盘：每进一次卡记一张凭证 collect_loot，报表拿它把吸取的盘点总数分到各张卡带上。

一次进卡从「卡名进来」开始，到下面任一刻交出（凭证里的 end）：
    complete   整卡正常完成（Collect_PackEnd_FeedCard，跟在完成写档后面）
    switched   同一个任务里下一张卡名进来了，上一张没正常离开（卡内失败、放弃）
    task_end   吸取任务走到正常结束点（LedgerPoint 的吸取结束位置，盘点之前）
    later      上一个任务中途停了，之后的进卡或记账位置替它交
一个采集点都没收过的那次进卡不交（没开「记录吸取掉落」时就是这样），所以没开时只多记一个卡名。

这里只管状态与凭证内容，不碰框架；store() 写存档，调用方要先确认账号。
"""

from datetime import datetime, timezone
import threading
import time

ENDS = ("complete", "switched", "task_end", "later")
END_LABEL = {"complete": "整卡完成", "switched": "没吸完就换卡", "task_end": "吸取收尾", "later": "上一个任务中途停了"}

_LOCK = threading.Lock()
_VISIT = None   # 正在记的这次进卡


def _iso(seconds) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


def _new(card, task, session, account, now, *, entered):
    return {"card": card, "task": {"entry": task.get("entry") or "", "task_id": task.get("task_id")},
            "session": session, "account": account, "entered_at": _iso(now) if entered else None, "points": []}


def _voucher(visit, end, now):
    """一次进卡 → 凭证；一个采集点都没收过返回 None。读到的条目按名字加总，字典里没有的另记。"""
    if not visit or not visit["points"]:
        return None
    items, unknown, unread, stuck = {}, {}, 0, 0
    for point in visit["points"]:
        for row in point.get("items") or ():
            name, count = row.get("name"), row.get("count")
            if not isinstance(name, str) or not name or type(count) is not int:
                continue
            bucket = items if row.get("known") else unknown
            bucket[name] = bucket.get(name, 0) + count
        unread += point.get("unread") or 0
        stuck += bool(point.get("stuck"))
    times = [point["time"] for point in visit["points"]]
    return {"card": visit["card"], "end": end, "session": visit["session"], "task": dict(visit["task"]),
            "account": visit["account"], "entered_at": visit["entered_at"], "first_at": _iso(min(times)),
            "last_at": _iso(max(times)), "at": _iso(now), "points": len(visit["points"]), "items": items,
            "unknown": unknown, "unread": unread, "stuck": stuck}


def _same_task(visit, task_id) -> bool:
    return visit["task"]["task_id"] == task_id


def enter(card, *, task, session=None, account=None, now=None):
    """卡名进来。与正在记的是同一张卡、同一个任务：什么都不做；否则交出上一次，从这里开始记这一张。

    返回要落盘的上一次进卡（凭证），没有就是 None。
    """
    global _VISIT
    now = time.time() if now is None else now
    with _LOCK:
        old = _VISIT
        if old is not None and old["card"] == card and _same_task(old, task.get("task_id")):
            return None
        _VISIT = _new(card, task, session, account, now, entered=True)
    if old is None:
        return None
    return _voucher(old, "switched" if _same_task(old, task.get("task_id")) else "later", now)


def add_point(record, *, card, task, session=None, account=None, now=None):
    """一个采集点收完（LootFeedCollect 的结果）。归到正在记的这次进卡。

    没赶上进卡（Agent 中途重启过）就从这里开一次；卡名读不到时并进当前这次；这次还没有卡名、这里读到了就补上。
    卡名或任务对不上：先交出旧的再开一次。返回要落盘的旧一次，没有就是 None。
    """
    global _VISIT
    now = time.time() if now is None else now
    task_id = task.get("task_id")
    replaced = None
    with _LOCK:
        if _VISIT is not None and (not _same_task(_VISIT, task_id)
                                   or (card is not None and _VISIT["card"] not in (None, card))):
            replaced, _VISIT = _VISIT, None
        if _VISIT is None:
            _VISIT = _new(card, task, session, account, now, entered=False)
        elif _VISIT["card"] is None and card is not None:
            _VISIT["card"] = card
        point = {key: record.get(key) for key in ("items", "unread", "stuck")}
        point["time"] = record.get("time") if isinstance(record.get("time"), (int, float)) else now
        _VISIT["points"].append(point)
    if replaced is None:
        return None
    return _voucher(replaced, "switched" if _same_task(replaced, task_id) else "later", now)


def leave(end, *, task_id=None, card=None, now=None):
    """交出正在记的这次进卡并清空。

    end 是 complete 或 task_end 时，这次进卡若不属于给出的任务，改记 later（上一个任务中途停了）；
    complete 给了卡名而与正在记的对不上，改记 switched（完成的不是它）。返回凭证或 None。
    """
    global _VISIT
    if end not in ENDS:
        raise ValueError(f"end 不对：{end!r}")
    now = time.time() if now is None else now
    with _LOCK:
        old, _VISIT = _VISIT, None
    if old is None:
        return None
    if end in ("complete", "task_end") and task_id is not None and not _same_task(old, task_id):
        end = "later"
    elif end == "complete" and card is not None and old["card"] not in (None, card):
        end = "switched"
    return _voucher(old, end, now)


def settle(task_id, *, task_end, now=None):
    """记账位置替还没交的进卡交出来，在留标记、盘点之前调，这次出的报表就用得上。

    吸取的正常结束位置（task_end=True）：交正在记的这一次（task_end；不是本任务的记 later）。
    别的位置：只交别的任务留下的（later，上一个任务中途停了），本任务正在记的不动。
    """
    global _VISIT
    now = time.time() if now is None else now
    with _LOCK:
        old = _VISIT
        if old is None or (not task_end and _same_task(old, task_id)):
            return None
        _VISIT = None
    return _voucher(old, "task_end" if task_end and _same_task(old, task_id) else "later", now)


def current():
    """正在记的这次进卡的副本（测试与日志用）。"""
    with _LOCK:
        if _VISIT is None:
            return None
        return {**_VISIT, "task": dict(_VISIT["task"]), "points": [dict(point) for point in _VISIT["points"]]}


def reset():
    global _VISIT
    with _LOCK:
        _VISIT = None


def describe(voucher) -> str:
    card = voucher.get("card") or "未识别卡带"
    return (f"{card}：{voucher['points']} 个采集点的掉落已落盘（{END_LABEL.get(voucher['end'], voucher['end'])}）"
            + (f"，{voucher['unread']} 条没读成" if voucher.get("unread") else "")
            + (f"，{voucher['stuck']} 个点列表卡住没救回" if voucher.get("stuck") else ""))


def store(voucher, account) -> str | None:
    """把交出来的一次进卡写进当前账号的存档。返回一句给日志的话；没东西可写返回 None。

    记的时候与写的时候账号不同（多存档切过号）就不写：凭证只能进它自己那个账号。
    调用方必须先 sync_from_context 确认账号。
    """
    if not voucher:
        return None
    if voucher.get("account") not in (None, account):
        return f"⚠️ {voucher.get('card') or '未识别卡带'} 的掉落是另一个账号记的，没有落盘"
    from .inventory_store import save_collect_loot   # 运行时才用到存档；本模块其余部分离线可测
    if not save_collect_loot(voucher):
        return f"⚠️ {voucher.get('card') or '未识别卡带'} 的掉落没写进存档"
    return describe(voucher)
