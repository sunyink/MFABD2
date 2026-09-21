"""按采购前确定的料理名单尝试补做，缺料沿原链返回并继续下一种。"""

from .cooking_stock import get_cooking_stock
from utils.account_sync import sync_from_context
from utils import arbitrage_store as store, mfaalog
from utils.arbitrage_recipe_catalog import (
    REPLENISH_END, discover_recipe_entries, build_replenish_selection,
)
from utils.name_i18n import canon


_START = "Arbitrage_Cooking_Replenish_Run"
_MENU = "Arbitrage_Cooking_Menu_Reset_SubOut"
_UNLIMITED = 2 ** 32 - 1


def select_cooking_targets(entries, planned_names):
    """只检查预定料理当前仍可执行，不按购买结果或记账库存重新筛选。"""
    if not isinstance(planned_names, (list, tuple)) or any(
            not isinstance(name, str) or not name.strip() for name in planned_names):
        raise ValueError("计划补做名单必须为料理名称列表")
    lookup = {entry.name: entry for entry in entries}
    result = {"status": "skipped", "targets": [], "skipped": {}}
    for name in dict.fromkeys(canon(name) for name in planned_names):
        entry = lookup.get(name)
        if entry is None or not entry.enabled or entry.reason:
            result["skipped"][name] = "recipe_disabled_or_unresolved"
        else:
            result["targets"].append(name)
    result["status"] = "planned" if result["targets"] else "skipped"
    return result


def _active(node):
    return node.get("enabled", True) and node.get("max_hit", _UNLIMITED) != 0


def execute_replenish_cooking(context, planned_names, *, day, bag_run_id, callback_task_id):
    """P5入口。started=True且returned=False时，调用者不得盲目回Hub继续出售。

    completed只表示目标队列已遍历且返回料理菜单，不声称已做出指定份数。
    不回灌整节点、不追加仓检；制作前作废与短缺采集沿用原链。
    """
    report = {"status": "skipped", "started": False, "returned": None, "targets": [], "visited": [],
              "selected": [], "observations": []}
    if type(callback_task_id) is not int or callback_task_id <= 0:
        return {**report, "status": "stopped", "reason": "invalid_callback_task_id"}
    if day != store.market_day():
        return {**report, "reason": "plan_day_changed"}
    if not planned_names:
        return report
    if context.tasker.stopping:
        return {**report, "status": "stopped", "reason": "task_stopping"}
    if not sync_from_context(context, where="ArbitrageReplenishCook"):
        return {**report, "status": "stopped", "reason": "account_sync_failed"}
    try:
        store.get_replenish_inventory(bag_run_id)
        entries = discover_recipe_entries(context)
        planned = select_cooking_targets(entries, planned_names)
        report.update(planned)
        selection = build_replenish_selection(entries, planned["targets"])
        if not selection.recipes:
            return report
        start = context.get_node_data(_START)
        if not isinstance(start, dict) or not _active(start):
            return {**report, "status": "skipped", "reason": "cooking_entry_disabled"}
        local = context.clone()
        overrides = dict(selection.pipeline_override)
        resets = list(selection.clear_hit_nodes)
        # 保留资源包原有菜单路径；清理实际可达菜单和目标料理的命中计数。
        # clone隔离节点覆盖但共享命中计数，只在启动前清理一次。
        for node_name in (_START,):
            node = context.get_node_data(node_name)
            if isinstance(node, dict) and _active(node) and 0 < node.get("max_hit", _UNLIMITED) < _UNLIMITED:
                resets.append(node_name)
        for node in dict.fromkeys(resets):
            if not local.clear_hit_count(node):
                raise RuntimeError(f"无法清除补做入口计数: {node}")
    except Exception as exc:
        return {**report, "status": "stopped", "reason": str(exc)}
    mfaalog.info("[ReplenishCook] 本轮补做：" + "、".join(report["targets"]))
    report.update(status="incomplete", started=True, returned=False)
    try:
        # Agent callbacks keep the outer task id; run_task returns a different child id.
        # Exclude observations already recorded by the first cooking pass in this task.
        previous_observations = get_cooking_stock(callback_task_id)
        report["observation_task_id"] = callback_task_id
        try:
            detail = local.run_task(_START, overrides)
        finally:
            report["observations"] = [row for row in get_cooking_stock(callback_task_id)
                                      if row not in previous_observations]
        if detail is not None:
            report["task_id"] = detail.task_id
            nodes = detail.nodes
            names = {node.name for node in nodes}
            completed = {node.name for node in nodes if node.completed}
            report["visited"] = [entry.name for entry in selection.recipes if entry.entry in names]
            selected = {node.name for node in nodes if node.action is not None and node.action.success}
            report["selected"] = [entry.name for entry in selection.recipes if selected.intersection(entry.selectors)]
            report["queue_completed"] = bool(detail.status.succeeded and REPLENISH_END in completed
                                              and len(report["visited"]) == len(selection.recipes))
        if context.tasker.stopping:
            return {**report, "status": "stopped", "reason": "task_stopping"}
        image = local.tasker.controller.post_screencap().wait().get()
        if image is None or not image.size:
            raise RuntimeError("补做收尾截图失败")
        returned = local.run_recognition(_MENU, image)
        report["returned"] = bool(returned is not None and returned.hit)
        if report.get("queue_completed") and report["returned"]:
            report["status"] = "completed"
        else:
            report["reason"] = "cooking_queue_or_return_unconfirmed"
    except Exception as exc:
        report["reason"] = str(exc)
    return report
