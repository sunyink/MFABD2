"""出售派发前，用库存记录挑掉确定没得卖的目标，不再每家柜台都去试（U62、U63）。

没得卖＝读到的数不超过保留量：①④ 料理保留 0，即读到 0；⑤ 材料按材料保留量。
只认这一段里读到的数：本次 Agent 会话里最近一次记账盘点开始之后——前提是本任务最近走过的记账位置
认这次盘点接得上（不用再盘，或当场盘了）。该盘没盘成、本任务没走过记账位置、没开记账、
那次盘点已滚出热档时，只认当前任务开始之后。更早的数可能被别的任务或手动游玩改过。
② 一键购买可能买进的商品在买后作废（ArbitrageBuyComplete），这里见到的已知数不会是买前的旧数。
数量未知、超过保留量的照旧去柜台，由出售子页重新读数核对。宁可多跑一趟，不误跳一个峰值。
"""

from datetime import datetime, timezone

from . import inventory_store, task_journal
from .inventory_store import moment_of
from .name_i18n import canon


SOURCES = {
    "bag_absent": "盘点判 0",
    "bag_detail": "盘点读数",
    "sale_confirmed": "卖后余量",
    "replenish_purchase": "补买后读数",
}


def trust_since(events=None, rows=None, session=None) -> datetime | None:
    """库存读数最早可以是什么时刻读到的；None 表示这次一个都不认。

    没有正在跑的任务（任务起止没接上）时不认。当前任务的行上有 stock_current（记账位置留的，task_journal.note）：
    是 True 才认本会话最近一次记账盘点的开始时刻（重扫并进原盘点，不另算）；是 False（该盘没盘成，
    最近那次盘点之后还有没盘掉的活动）或没有（本任务没走过记账位置），以及找不到盘点时，取当前任务的开始时刻。
    """
    rows = task_journal.rows() if rows is None else rows
    running = [row for row in rows if row.get("end") is None and isinstance(row.get("start"), (int, float))]
    if not running:
        return None
    task_start = datetime.fromtimestamp(running[-1]["start"], timezone.utc)
    if running[-1].get("stock_current") is not True:
        return task_start
    session = task_journal.SESSION if session is None else session
    events = inventory_store.get_events() if events is None else events
    starts = []
    for event in events:
        if not isinstance(event, dict):
            continue
        origin = event.get("origin")
        if (event.get("kind") == "inventory_scan" and isinstance(origin, dict)
                and origin.get("session") == session and not origin.get("retry_of")):
            moment = moment_of(event.get("started_at"))
            if moment is not None:
                starts.append(moment)
    return max(starts) if starts else task_start


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
        seen = moment_of(fact.get("quantity_observed_at"))
        if seen is None or seen < since:
            continue
        source = str(fact.get("quantity_source") or "?")
        result[name] = (SOURCES.get(source, source), seen, quantity)
    return result
