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
    "scan_id", "label", "note", "started_at", "finished_at", "job_id", "profile", "target_count",
    "complete", "termination", "continuity", "pages", "quantities", "absent", "unknown", "timing_ms",
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


def get_latest_scan(label: str) -> dict | None:
    """读取某个标签最近一次扫描的摘要副本；没有则返回 None。"""
    with _base._LOCK:
        latest = _base._inventory(_base.PersistentStore.load())["latest"]
        summary = latest.get("scans", {}).get(label) if isinstance(latest.get("scans"), dict) else None
        return copy.deepcopy(summary) if isinstance(summary, dict) else None
