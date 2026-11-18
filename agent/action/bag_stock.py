"""商店套利 ③ 的仓检适配：本轮还缺读数的食材交给通用背包仓检（bag_scan.py）读取，
再按旧形状写本轮汇总 latest.bag，补买补做照常经 get_bag_scan_run / get_replenish_inventory 读取。"""

from collections import OrderedDict
from datetime import datetime

from utils.inventory_store import moment_of

from .bag_scan import profile_names


_BAG_RUNS = OrderedDict()


def get_bag_scan_run(task_id):
    """当前进程本任务最后一次仓检的编号；无本次扫描时不能消费旧存档汇总。"""
    return _BAG_RUNS.get(task_id)


def forget_bag_run(task_id):
    _BAG_RUNS.pop(task_id, None)


def register_bag_run(task_id, run_id):
    _BAG_RUNS[task_id] = run_id
    _BAG_RUNS.move_to_end(task_id)
    while len(_BAG_RUNS) > 16:
        _BAG_RUNS.popitem(last=False)


def bag_catalog(context) -> dict[str, str]:
    """通用仓检能盘点的食材：规范名 → 空串（沿用旧的字典形状）。物品表里的食材都能盘点，没有图标的靠点开读名。"""
    return dict.fromkeys(profile_names("ingredients"), "")


def pending_materials(catalog: dict, items: dict, since: datetime | None) -> list[str]:
    """只复用 since 之后读到、还没被作废的精确量；since 为 None（单独运行背包）就全部重查。

    since 由调用方定。没时区的读数时刻不猜，当作要重查。
    """
    pending = []
    for name in catalog:
        fact = items.get(name, {})
        quantity = fact.get("quantity")
        seen = moment_of(fact.get("quantity_observed_at"))
        if not (since is not None and fact.get("quantity_status") == "known"
                and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0
                and seen is not None and seen > since):
            pending.append(name)
    return pending


def bag_summary(task_id, run_id, observed_at, required, pending, catalog, scan):
    """把通用仓检的结果折成补买读口认的旧形状；scan 为 None 表示本轮不需要进包。"""
    pending = set(pending)
    read = sorted(set((scan or {}).get("quantities") or {}) & pending)
    absent = sorted(set((scan or {}).get("absent") or []) & pending)
    errors = {name: "template_missing" for name in sorted(pending - catalog.keys())}
    if scan is not None:
        errors.update({name: reason for name, reason in (scan.get("unknown") or {}).items() if name in pending})
        if not scan.get("executed", True):
            errors["scan"] = scan.get("error") or "not_executed"
        elif scan.get("error"):
            errors["scan"] = scan["error"]
    unknown = sorted(pending - set(read) - set(absent))
    return {
        "observed_at": observed_at, "complete": not unknown,
        "read_item_names": read, "absent_item_names": absent, "unknown_item_names": unknown,
        "reused_item_names": sorted(set(required) - pending), "errors": errors,
        "pages_scanned": int((scan or {}).get("pages") or 0), "task_id": task_id, "run_id": run_id,
        "scan_id": (scan or {}).get("scan_id"),
    }
