"""商店套利的持久化边界：共享行情、账号库存与事实观察。"""

import copy
import threading
from datetime import datetime, timezone

from . import mfaalog
from .name_i18n import canon
from .persistent_store import PersistentStore, SharedStore


SCHEMA_VERSION = 2
MARKET_PARSER_VERSION = 3
MARKET_RETENTION_DAYS = 62
OBSERVATION_LIMIT = 200

_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def market_day() -> str:
    """行情缓存暂以 UTC 日期分桶；商店实际换日时刻尚待核实，交易仍须子页核价。"""
    return datetime.now(timezone.utc).date().isoformat()


def get_purchase_alignments() -> dict:
    """当前账号各卡带已成功应用的收藏名单；不从共享存档借用。"""
    with _LOCK:
        rows = _root(PersistentStore.load()).get("purchase_alignments", {})
        if not isinstance(rows, dict):
            raise ValueError("采购收藏记录格式错误")
        return copy.deepcopy(rows)


def save_purchase_alignment(cartridge: str, items) -> bool:
    """仅由收藏实际核对成功的回调调用；失败不更新该卡带基准。"""
    with _LOCK:
        data = PersistentStore.load()
        rows = _dict_child(_root(data), "purchase_alignments")
        rows[cartridge] = {"items": sorted(set(items))}
        return bool(PersistentStore.save(data))


def invalidate_purchase_alignment(cartridge: str) -> bool:
    """重新核对前撤销旧基准，避免部分点星后失败仍复用旧成功记录。"""
    return invalidate_purchase_alignments((cartridge,))


def invalidate_purchase_alignments(cartridges) -> bool:
    """全扫前一次撤销旧清单；漏扫卡带下次仍按无记录重试。"""
    with _LOCK:
        data = PersistentStore.load()
        rows = _dict_child(_root(data), "purchase_alignments")
        stale = set(cartridges) & rows.keys()
        if not stale:
            return True
        for cartridge in stale:
            del rows[cartridge]
        return bool(PersistentStore.save(data))


def _dict_child(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _list_child(parent: dict, key: str) -> list:
    value = parent.get(key)
    if not isinstance(value, list):
        value = []
        parent[key] = value
    return value


def _root(data: dict) -> dict:
    root = _dict_child(data, "arbitrage")
    root["schema_version"] = SCHEMA_VERSION
    return root


def _trim_mapping(mapping: dict, limit: int) -> None:
    for key in sorted(mapping)[:-limit]:
        del mapping[key]


def _trim_list(items: list, limit: int) -> None:
    if len(items) > limit:
        del items[:-limit]


def _normalized_market_items(items: list[dict]) -> list[dict]:
    normalized = {}
    optional = (
        "current_price",
        "peak_price",
        "current_rate",
        "peak_rate",
        "max_price_basis",
        "target_cartridge",
        "cart_score",
        "cart_conflict",
        "alt_cartridge",
        "current_cartridge",
        "current_cartridge_raw",
        "monthly_cartridge",
        "cartridge_read_basis",
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name", "")).strip()
        name = canon(raw_name)
        if not name:
            continue
        saved = {"name": name, "is_max_price": bool(item.get("is_max_price"))}
        if raw_name != name:
            saved["raw_name"] = raw_name
        for key in optional:
            if key in item and item[key] not in (None, ""):
                saved[key] = copy.deepcopy(item[key])
        normalized[name] = saved
    return list(normalized.values())


def get_market_snapshot(day: str | None = None) -> dict | None:
    """读取当天完整且由当前解析器生成的共享行情；旧版上下行择优缓存需重扫。"""
    with _LOCK:
        data = SharedStore.load()
        days = _dict_child(_dict_child(_root(data), "market"), "days")
        snapshot = days.get(day or market_day())
        if (not isinstance(snapshot, dict) or not snapshot.get("complete")
                or snapshot.get("parser_version") != MARKET_PARSER_VERSION):
            return None
        return copy.deepcopy(snapshot)


def save_market_snapshot(scan: dict, day: str | None = None) -> bool:
    """保存所有物品视图里的当天行情；完整缓存不会被失败重扫覆盖。"""
    snapshot = {
        "day": day or market_day(),
        "parser_version": MARKET_PARSER_VERSION,
        "observed_at": scan.get("observed_at") or utc_now(),
        "scope": "all_sellable_items",
        "complete": bool(scan.get("complete")),
        "termination_reason": scan.get("termination_reason", "unknown"),
        "pages_scanned": int(scan.get("pages_scanned", 0)),
        "items": _normalized_market_items(scan.get("items") or []),
    }
    with _LOCK:
        data = SharedStore.load()
        market = _dict_child(_root(data), "market")
        days = _dict_child(market, "days")
        previous = days.get(snapshot["day"])
        if not (isinstance(previous, dict) and previous.get("complete") and not snapshot["complete"]):
            days[snapshot["day"]] = snapshot
        attempts = _list_child(market, "attempts")
        attempts.append({
            "day": snapshot["day"],
            "observed_at": snapshot["observed_at"],
            "complete": snapshot["complete"],
            "termination_reason": snapshot["termination_reason"],
            "pages_scanned": snapshot["pages_scanned"],
            "item_count": len(snapshot["items"]),
        })
        _trim_mapping(days, MARKET_RETENTION_DAYS)
        _trim_list(attempts, MARKET_RETENTION_DAYS * 3)
        return bool(SharedStore.save(data))


def _inventory(data: dict) -> dict:
    inventory = _dict_child(_root(data), "inventory")
    inventory["schema_version"] = SCHEMA_VERSION
    _dict_child(inventory, "items")
    latest = _dict_child(inventory, "latest")
    _dict_child(latest, "cooking")
    # observations 是 v1 留下的完整观察历史。保留已有内容以免升级时丢资料，
    # 但 v2 不再追加大块 OCR 明细；最新状态走 latest，历史只追加紧凑 events。
    _list_child(inventory, "observations")
    _list_child(inventory, "events")
    return inventory


def save_possession_snapshot(scan: dict) -> bool:
    """记录价目表证明存在的物品；该页面没有数量，绝不补零或抹掉精确数量。"""
    observed_at = scan.get("observed_at") or utc_now()
    items = _normalized_market_items(scan.get("items") or [])
    with _LOCK:
        data = PersistentStore.load()
        inventory = _inventory(data)
        current = inventory["items"]
        for item in items:
            fact = _dict_child(current, item["name"])
            fact["present"] = True
            fact["presence_observed_at"] = observed_at
            fact["presence_source"] = "sell_price_list"
        item_names = [item["name"] for item in items]
        peak_names = [item["name"] for item in items if item.get("is_max_price")]
        coverage = {
            "observed_at": observed_at,
            "complete": bool(scan.get("complete")),
            "sale_candidates_complete": bool(scan.get(
                "sale_candidates_complete", scan.get("complete")
            )),
            "full_list_complete": bool(scan.get("full_list_complete", False)),
            "termination_reason": scan.get("termination_reason", "unknown"),
            "pages_scanned": int(scan.get("pages_scanned", 0)),
            "target_rate_floor": scan.get("target_rate_floor"),
            "lowest_observed_rate": scan.get("lowest_observed_rate"),
            "rate_order_safe": scan.get("rate_order_safe"),
            "item_names": item_names,
            "peak_item_names": peak_names,
        }
        _dict_child(inventory, "latest")["possession"] = coverage
        inventory["events"].append({
            "kind": "possession_scan",
            "observed_at": observed_at,
            "complete": coverage["complete"],
            "sale_candidates_complete": coverage["sale_candidates_complete"],
            "full_list_complete": coverage["full_list_complete"],
            "termination_reason": coverage["termination_reason"],
            "pages_scanned": coverage["pages_scanned"],
            "target_rate_floor": coverage["target_rate_floor"],
            "item_count": len(item_names),
            "peak_item_count": len(peak_names),
        })
        _trim_list(inventory["events"], OBSERVATION_LIMIT)
        return bool(PersistentStore.save(data))


def save_cooking_stock_observation(record: dict) -> bool:
    """保存料理菜单业务状态；OCR 候选等排错明细由识别 details 承担。"""
    observed_at = record.get("observed_at") or utc_now()
    with _LOCK:
        data = PersistentStore.load()
        inventory = _inventory(data)
        current = inventory["items"]
        readable_items = []
        material_names = []
        for material in record.get("materials") or []:
            if not isinstance(material, dict):
                continue
            name = canon(str(material.get("name") or "").strip())
            if name and name not in material_names:
                material_names.append(name)
            quantity = material.get("stock")
            if not name or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                continue
            fact = _dict_child(current, name)
            fact.update({
                "quantity": quantity,
                "quantity_status": "known",
                "quantity_observed_at": observed_at,
                "quantity_source": "cooking_menu",
                "present": quantity > 0,
            })
            readable_items.append(name)

        recipe = canon(str(record.get("recipe") or "").strip())
        attempt = {
            "last_attempt_at": observed_at,
            "complete": bool(record.get("complete")),
            "selected_count": record.get("selected_count"),
            "material_names": material_names,
            "readable_item_names": readable_items,
        }
        latest = _dict_child(inventory, "latest")
        if recipe:
            cooking = _dict_child(latest, "cooking")
            previous = cooking.get(recipe)
            if attempt["complete"]:
                attempt["last_complete_at"] = observed_at
            elif isinstance(previous, dict) and previous.get("last_complete_at"):
                attempt["last_complete_at"] = previous["last_complete_at"]
            cooking[recipe] = attempt
        else:
            # 菜名没读出来时没有安全的 recipe key；只留最近一次未知尝试的状态。
            latest["cooking_unknown"] = attempt

        inventory["events"].append({
            "kind": "cooking_stock",
            "observed_at": observed_at,
            "recipe": recipe or None,
            "complete": attempt["complete"],
            "readable_item_names": readable_items,
            "error_count": len(record.get("errors") or []),
        })
        _trim_list(inventory["events"], OBSERVATION_LIMIT)
        return bool(PersistentStore.save(data))


def set_inventory_quantities(quantities: dict[str, int], reason: str,
                             reference: dict | None = None) -> bool:
    """用执行后可确认的绝对数量覆盖当前库存，并留一条审计事件。"""
    observed_at = utc_now()
    normalized = {}
    for raw_name, quantity in quantities.items():
        name = canon(str(raw_name).strip())
        if not name or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            continue
        normalized[name] = quantity
    if not normalized:
        return True

    with _LOCK:
        data = PersistentStore.load()
        inventory = _inventory(data)
        current = inventory["items"]
        for name, quantity in normalized.items():
            fact = _dict_child(current, name)
            fact.update({
                "quantity": quantity,
                "quantity_status": "known",
                "quantity_observed_at": observed_at,
                "quantity_source": reason,
                "present": quantity > 0,
            })
        event = {
            "kind": "inventory_set",
            "observed_at": observed_at,
            "reason": reason,
            "quantities": normalized,
        }
        if reference:
            event["reference"] = copy.deepcopy(reference)
        inventory["events"].append(event)
        _trim_list(inventory["events"], OBSERVATION_LIMIT)
        return bool(PersistentStore.save(data))


def get_inventory_items() -> dict:
    """读取当前账号逐物品库存的副本，未知状态由调用方显式处理。"""
    with _LOCK:
        return copy.deepcopy(_inventory(PersistentStore.load())["items"])


def get_replenish_inventory(bag_run_id: str) -> dict:
    """只接受当前仓检尝试的汇总，按项取已确认量；旧存档与未知量不补成0。"""
    if not isinstance(bag_run_id, str) or not bag_run_id:
        raise ValueError("缺少当前仓检运行编号")
    with _LOCK:
        inventory = _inventory(PersistentStore.load())
        bag = inventory.get("latest", {}).get("bag", {})
        if not isinstance(bag, dict) or bag.get("run_id") != bag_run_id:
            raise ValueError("未取得当前仓检汇总")
        observed = set()
        for field in ("read_item_names", "absent_item_names", "reused_item_names"):
            observed.update(bag.get(field, []))
        unknown = set(bag.get("unknown_item_names", []))
        quantities = {}
        for name in sorted(observed - unknown):
            fact = inventory["items"].get(name, {})
            value = fact.get("quantity")
            if fact.get("quantity_status") == "known" and type(value) is int and value >= 0:
                quantities[name] = value
            else:
                unknown.add(name)
        return {"bag": copy.deepcopy(bag), "quantities": quantities, "unknown": sorted(unknown)}


def save_bag_stock_summary(record: dict) -> bool:
    """背包补查只保存覆盖情况；逐项数量通过统一 inventory_set 入口即时落盘。"""
    with _LOCK:
        data = PersistentStore.load()
        inventory = _inventory(data)
        summary = copy.deepcopy(record)
        inventory["latest"]["bag"] = summary
        inventory["events"].append({
            "kind": "bag_stock", "observed_at": summary["observed_at"],
            "complete": summary["complete"],
            "read_count": len(summary["read_item_names"]),
            "absent_count": len(summary["absent_item_names"]),
            "unknown_item_names": summary["unknown_item_names"],
        })
        _trim_list(inventory["events"], OBSERVATION_LIMIT)
        return bool(PersistentStore.save(data))


def invalidate_inventory_quantities(names: list[str], reason: str,
                                    reference: dict | None = None) -> bool:
    """执行已改变库存但数量未知时，作废旧数量；不把“卖过”猜成“卖光”。"""
    observed_at = utc_now()
    normalized = []
    seen = set()
    for raw_name in names:
        name = canon(str(raw_name).strip())
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    if not normalized:
        return True

    with _LOCK:
        data = PersistentStore.load()
        inventory = _inventory(data)
        current = inventory["items"]
        for name in normalized:
            fact = _dict_child(current, name)
            fact.pop("quantity", None)
            fact.pop("quantity_observed_at", None)
            fact.pop("present", None)
            fact["quantity_status"] = "unknown"
            fact["quantity_source"] = f"invalidated:{reason}"
            fact["quantity_invalidated_at"] = observed_at
        event = {
            "kind": "inventory_invalidate",
            "observed_at": observed_at,
            "reason": reason,
            "items": normalized,
        }
        if reference:
            event["reference"] = copy.deepcopy(reference)
        inventory["events"].append(event)
        _trim_list(inventory["events"], OBSERVATION_LIMIT)
        return bool(PersistentStore.save(data))


def apply_inventory_delta(changes: dict[str, int], reason: str, reference: dict | None = None) -> dict:
    """为购买、制作、出售预留的统一库存更新入口；未知基数只记事件，不猜当前量。"""
    observed_at = utc_now()
    result = {"applied": {}, "unknown": [], "conflicts": []}
    normalized_changes = {}
    for raw_name, delta in changes.items():
        name = canon(str(raw_name).strip())
        if not name or not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
            continue
        normalized_changes[name] = normalized_changes.get(name, 0) + delta

    with _LOCK:
        data = PersistentStore.load()
        inventory = _inventory(data)
        current = inventory["items"]
        for name, delta in normalized_changes.items():
            fact = _dict_child(current, name)
            quantity = fact.get("quantity")
            if not isinstance(quantity, int) or isinstance(quantity, bool):
                fact["quantity_status"] = "unknown"
                result["unknown"].append(name)
                continue
            new_quantity = quantity + delta
            if new_quantity < 0:
                fact.pop("quantity", None)
                fact["quantity_status"] = "unknown"
                result["conflicts"].append(name)
                continue
            fact.update({
                "quantity": new_quantity,
                "quantity_status": "known",
                "quantity_observed_at": observed_at,
                "quantity_source": f"delta:{reason}",
                "present": new_quantity > 0,
            })
            result["applied"][name] = new_quantity
        event = {
            "kind": "inventory_delta",
            "observed_at": observed_at,
            "reason": reason,
            "changes": normalized_changes,
            "result": copy.deepcopy(result),
        }
        if reference:
            event["reference"] = copy.deepcopy(reference)
        inventory["events"].append(event)
        _trim_list(inventory["events"], OBSERVATION_LIMIT)
        if not PersistentStore.save(data):
            mfaalog.error("[ArbitrageStore] 库存变化写入失败")
    return result
