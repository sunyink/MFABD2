"""出售派发前，用库存记录挑掉确定没得卖的目标，不再每家柜台都去试（U62、U63）。

没得卖＝读到的数不超过保留量：①④ 料理保留 0，即读到 0；⑤ 材料按材料保留量。
只认这一段里读到的数：本次 Agent 会话里最近一次记账盘点开始之后；没开记账、或那次盘点
已滚出热档时，只认当前任务开始之后。更早的数可能被别的任务或手动游玩改过。
② 一键购买可能买进的商品在买后作废（ArbitrageBuyComplete），这里见到的已知数不会是买前的旧数。
数量未知、超过保留量的照旧去柜台，由出售子页重新读数核对。宁可多跑一趟，不误跳一个峰值。
"""

from datetime import datetime, timezone

from . import inventory_store, task_journal
from .name_i18n import canon


SOURCES = {
    "bag_absent": "盘点判 0",
    "bag_detail": "盘点读数",
    "sale_confirmed": "卖后余量",
    "replenish_purchase": "补买后读数",
}


def _moment(text) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo is not None else None


def trust_since(events=None, rows=None, session=None) -> datetime | None:
    """库存读数最早可以是什么时刻读到的；None 表示这次一个都不认。

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


def known_within(reserves: dict, items: dict, since: datetime | None) -> dict:
    """{名字: (来源说明, 读到的时刻, 数量)}：库存记录是 since 之后读到、不超过保留量的那些目标。

    reserves 是 {名字: 保留量}；保留量不是非负整数的目标一律不挑。
    """
    if since is None:
        return {}
    result = {}
    for name, reserve in reserves.items():
        fact = items.get(canon(name))
        if not isinstance(fact, dict) or fact.get("quantity_status") != "known":
            continue
        quantity = fact.get("quantity")
        if type(quantity) is not int or type(reserve) is not int or not 0 <= quantity <= reserve:
            continue
        seen = _moment(fact.get("quantity_observed_at"))
        if seen is None or seen < since:
            continue
        source = str(fact.get("quantity_source") or "?")
        result[name] = (SOURCES.get(source, source), seen, quantity)
    return result
