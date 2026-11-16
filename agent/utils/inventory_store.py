"""通用库存读写入口：背包仓检等不属于套利的调用方从这里读写。

库存实体仍在账号存档的 arbitrage.inventory（历史位置，不迁移）。读写原语由
arbitrage_store 实现；这里一律经模块属性访问它，测试对 arbitrage_store 里
PersistentStore、utc_now 的替换对本模块同样生效。
"""

import copy

from . import arbitrage_store as _base
from .arbitrage_store import (  # noqa: F401  通用读写原样转出
    get_inventory_items,
    invalidate_inventory_quantities,
    set_inventory_quantities,
)


_RECORD_FIELDS = (
    "scan_id", "label", "note", "started_at", "finished_at", "job_id", "profile", "scope", "target_count",
    "complete", "termination", "continuity", "pages", "quantities", "absent", "unknown", "cells", "timing_ms",
    "origin", "corner",
)


def utc_now() -> str:
    return _base.utc_now()


def save_scan_record(record: dict) -> dict | None:
    """保存一次带标签的扫描记录：热档按标签只留最近一次摘要，完整记录作为事件进冷档。

    数量已由逐页 set_inventory_quantities 写入；本事件不再附带 item_facts。
    返回带账号内序号 seq 的事件副本，保存失败返回 None。
    """
    label = record.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("扫描记录缺少标签")
    with _base._LOCK:
        data = _base.PersistentStore.load()
        inventory = _base._inventory(data)
        latest = inventory["latest"]
        previous = latest.get("scan_seq")
        seq = previous + 1 if type(previous) is int and previous >= 0 else 1
        latest["scan_seq"] = seq
        event = {key: copy.deepcopy(record[key]) for key in _RECORD_FIELDS if key in record}
        event.update(kind="inventory_scan", seq=seq,
                     observed_at=record.get("finished_at") or _base.utc_now())
        _base._dict_child(latest, "scans")[label] = {
            "scan_id": event.get("scan_id"),
            "seq": seq,
            "started_at": event.get("started_at"),
            "finished_at": event.get("finished_at"),
            "complete": bool(event.get("complete")),
            "termination": event.get("termination"),
            "read_count": len(event.get("quantities") or {}),
            "absent_count": len(event.get("absent") or []),
            "unknown_item_names": sorted(event.get("unknown") or {}),
        }
        inventory["events"].append(event)
        if not _base._save_inventory(data):
            return None
        return copy.deepcopy(event)


def get_events() -> list:
    """热档里的事件副本。出报表时与冷档合起来就是该账号的全部凭证。"""
    with _base._LOCK:
        return copy.deepcopy(_base._inventory(_base.PersistentStore.load())["events"])


def get_latest_scan(label: str) -> dict | None:
    """读取某个标签最近一次扫描的摘要副本；没有则返回 None。"""
    with _base._LOCK:
        latest = _base._inventory(_base.PersistentStore.load())["latest"]
        summary = latest.get("scans", {}).get(label) if isinstance(latest.get("scans"), dict) else None
        return copy.deepcopy(summary) if isinstance(summary, dict) else None


_CURRENCY_FIELDS = (
    "label", "observed_at", "gold", "diamond", "tonic", "diamond_free", "diamond_paid", "diamond_paid_source",
    "icons_found", "detail_error", "detail_closed", "scan_id",
)


def save_currency_reading(record: dict) -> bool:
    """保存一次顶栏货币读数（钻石、金币、天赋神药，及钻石免费 / 付费）：事件进冷档，热档 latest.currency 留最近一次。

    读不到的项为 None，原样记下，不补零。货币不是背包物品，不写进 items。
    """
    event = {key: copy.deepcopy(record.get(key)) for key in _CURRENCY_FIELDS}
    event["kind"] = "currency_reading"
    event["observed_at"] = event["observed_at"] or _base.utc_now()
    with _base._LOCK:
        data = _base.PersistentStore.load()
        inventory = _base._inventory(data)
        inventory["latest"]["currency"] = {key: value for key, value in event.items() if key != "kind"}
        inventory["events"].append(event)
        return bool(_base._save_inventory(data))


_MARK_FIELDS = ("mark", "type", "at", "session", "task", "node", "tasks", "cards")


def save_ledger_mark(record: dict) -> bool:
    """保存一条记账标记（流程走到了某个位置）：只进事件流，不动 items 与 latest。

    标记只记事实——哪个节点、哪个任务、什么类型、什么时刻，以及上一条标记以来框架报过的任务起止；
    盘没盘、算谁的都不在这里（盘点记录自己带 origin 指回触发它的位置）。
    """
    event = {key: copy.deepcopy(record[key]) for key in _MARK_FIELDS if key in record}
    event["kind"] = "ledger_mark"
    event["observed_at"] = event.get("at") or _base.utc_now()
    with _base._LOCK:
        data = _base.PersistentStore.load()
        _base._inventory(data)["events"].append(event)
        return bool(_base._save_inventory(data))


_LOOT_FIELDS = ("card", "end", "session", "task", "entered_at", "first_at", "last_at", "at", "points", "items",
                "unknown", "unread", "stuck")


def save_collect_loot(record: dict) -> bool:
    """保存一次进卡的吸取掉落（每进一次卡一条，utils/loot_cards.py 交出来的）：只进事件流，不动 items 与 latest。

    数量是放技能时左侧获得列表读到的，不是库存读数；报表只拿它把吸取的盘点总数分到各张卡带上。
    """
    event = {key: copy.deepcopy(record[key]) for key in _LOOT_FIELDS if key in record}
    event["kind"] = "collect_loot"
    event["observed_at"] = event.get("at") or _base.utc_now()
    with _base._LOCK:
        data = _base.PersistentStore.load()
        _base._inventory(data)["events"].append(event)
        return bool(_base._save_inventory(data))
