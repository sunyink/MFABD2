"""出售派发前，用库存记录挑掉确定没货的目标，不再每家柜台都去试（U62）。

只认这一段里读到的 0：本次 Agent 会话里最近一次记账盘点开始之后；没开记账、或那次盘点
已滚出热档时，只认当前任务开始之后。更早的 0 可能被别的任务或手动游玩改过。
数量未知、大于 0 的照旧去柜台，由出售子页重新读数核对。宁可多跑一趟，不误跳一个峰值。
"""

from datetime import datetime, timezone

from . import inventory_store, task_journal
from .name_i18n import canon


SOURCES = {
    "bag_absent": "盘点判 0",
    "bag_detail": "盘点读到 0",
    "sale_confirmed": "卖光了",
}


def _moment(text) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else None


def trust_since(events=None, rows=None, session=None) -> datetime | None:
    """0 最早可以是什么时刻读到的；None 表示这次一个 0 都不认。

    没有正在跑的任务（任务起止没接上）时不认；有本会话的记账盘点时取最近一次的开始时刻
    （重扫并进原盘点，不另算）；没有就取当前任务的开始时刻。
    """
    rows = task_journal.rows() if rows is None else rows
    running = [row for row in rows if row.get("end") is None and isinstance(row.get("start"), (int, float))]
    if not running:
        return None
    session = task_journal.SESSION if session is None else session
    events = inventory_store.get_events() if events is None else events
    starts = []
    for event in events:
        if not isinstance(event, dict):
            continue
        origin = event.get("origin")
        if (event.get("kind") == "inventory_scan" and isinstance(origin, dict)
                and origin.get("session") == session and not origin.get("retry_of")):
            moment = _moment(event.get("started_at"))
            if moment is not None:
                starts.append(moment)
    if starts:
        return max(starts)
    return datetime.fromtimestamp(running[-1]["start"], timezone.utc)


def known_empty(names, items: dict, since: datetime | None) -> dict:
    """{名字: (来源说明, 读到的时刻)}：库存记录是 since 之后读到的 0 的那些目标。"""
    if since is None:
        return {}
    result = {}
    for name in names:
        fact = items.get(canon(name))
        if not isinstance(fact, dict) or fact.get("quantity_status") != "known":
            continue
        quantity = fact.get("quantity")
        if type(quantity) is not int or quantity != 0:
            continue
        seen = _moment(fact.get("quantity_observed_at"))
        if seen is None or seen < since:
            continue
        source = str(fact.get("quantity_source") or "?")
        result[name] = (SOURCES.get(source, source), seen)
    return result
