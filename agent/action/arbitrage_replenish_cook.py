"""按实际补买与当前库存筛选今日待售料理，局部复用原MAX制作链。"""

from .cooking_stock import get_cooking_stock
from utils.account_sync import sync_from_context
from utils import arbitrage_store as store, mfaalog
from utils.arbitrage_recipe_catalog import discover_recipe_entries, build_replenish_selection
from utils.arbitrage_replenish_plan import _market_prices
from utils.name_i18n import canon


_START = "Arbitrage_Cooking_MenuPatch"
_PAGE = "Arbitrage_Cooking_Page1"
_END = "Arbitrage_Cooking_ERREND"
_MENU = "Arbitrage_Cooking_Menu_Reset_SubOut"
_UNLIMITED = 2 ** 32 - 1


def select_cooking_targets(entries, purchases, quantities, market, data, *, day, sell_names):
    """不分配预测份数；共享材料最终按原队列MAX和短缺分支裁决。"""
    result = {"status": "skipped", "targets": [], "skipped": {}}
    if purchases.get("status") != "completed" or purchases.get("day") != day:
        return {**result, "reason": "purchases_not_completed_today"}
    if not isinstance(market, dict) or market.get("day") != day or market.get("complete") is not True:
        return {**result, "reason": "current_market_incomplete"}
    bought = {canon(name) for name, amount in purchases.get("purchased", {}).items()
              if type(amount) is int and amount > 0}
    if not bought:
        return {**result, "reason": "nothing_purchased"}
    if isinstance(sell_names, (str, bytes)):
        raise ValueError("最终出售清单必须为名称集合")
    sell_names = {canon(name) for name in sell_names}
    prices, peaks = _market_prices(market)
    for entry in entries:
        name = entry.name
        recipe = data["recipes"].get(name)
        reason = None
        if not entry.enabled or entry.reason:
            reason = "recipe_disabled_or_unresolved"
        elif not recipe:
            reason = "recipe_data_missing"
        elif name not in sell_names or name not in peaks:
            reason = "not_today_peak_sale"
        elif not bought.intersection(recipe["ingredients"]):
            reason = "not_related_to_actual_purchase"
        elif any(type(quantities.get(mat)) is not int for mat in recipe["ingredients"]):
            reason = "inventory_unknown"
        elif any(quantities[mat] < count for mat, count in recipe["ingredients"].items()):
            reason = "insufficient_actual_inventory"
        elif any(mat not in prices for mat in recipe["ingredients"]):
            reason = "material_peak_missing"
        elif prices[name] <= sum(prices[mat] * count for mat, count in recipe["ingredients"].items()) + (
            recipe["level"] * data["tonic_unit_price"]
        ):
            reason = "cooking_not_profitable"
        if reason:
            result["skipped"][name or entry.entry] = reason
        else:
            result["targets"].append(name)
    result["status"] = "planned" if result["targets"] else "skipped"
    return result


def _active(node):
    return node.get("enabled", True) and node.get("max_hit", _UNLIMITED) != 0


def execute_replenish_cooking(context, purchases, market, data, *, bag_run_id, sell_names):
    """P5入口。started=True且returned=False时，调用者不得盲目回Hub继续出售。

    completed只表示目标队列已遍历且返回料理菜单，不声称已做出指定份数。
    不回灌整节点、不追加仓检；制作前作废与短缺采集沿用原链。
    """
    report = {"status": "skipped", "started": False, "returned": None, "targets": [], "visited": [],
              "selected": [], "observations": []}
    if (not isinstance(purchases, dict) or purchases.get("bag_run_id") != bag_run_id
            or purchases.get("day") != store.market_day() or purchases.get("status") != "completed"):
        return {**report, "reason": "purchase_context_mismatch"}
    if context.tasker.stopping:
        return {**report, "status": "stopped", "reason": "task_stopping"}
    if not sync_from_context(context, where="ArbitrageReplenishCook"):
        return {**report, "status": "stopped", "reason": "account_sync_failed"}
    try:
        inventory = store.get_replenish_inventory(bag_run_id)["quantities"]
        for name, amount in purchases.get("purchased", {}).items():
            if (type(amount) is not int or amount <= 0 or name not in purchases.get("inventory_after", {})
                    or inventory.get(name) != purchases["inventory_after"][name]):
                return {**report, "reason": "purchase_inventory_changed"}
        entries = discover_recipe_entries(context)
        planned = select_cooking_targets(entries, purchases, inventory, market, data,
                                         day=store.market_day(), sell_names=sell_names)
        report.update(planned)
        selection = build_replenish_selection(entries, planned["targets"])
        if not selection.recipes:
            return report
        start = context.get_node_data(_START)
        if not isinstance(start, dict) or not _active(start):
            return {**report, "status": "skipped", "reason": "cooking_entry_disabled"}
        local = context.clone()
        overrides = dict(selection.pipeline_override)
        # 保留终点的流转语义，仅取消常规每周标记；外层配置不受影响。
        overrides[_END] = {"action": "DoNothing"}
        resets = list(selection.clear_hit_nodes)
        # 五星目标也会经过第一页收尾；不能只清目标菜谱自身的祖先。
        for node_name in (_START, _PAGE):
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
        detail = local.run_task(_START, overrides)
        if detail is not None:
            report["task_id"] = detail.task_id
            nodes = [node for node in detail.nodes if node.completed]
            names = {node.name for node in nodes}
            report["visited"] = [entry.name for entry in selection.recipes if entry.entry in names]
            selected = {node.name for node in nodes if node.action is not None and node.action.success}
            report["selected"] = [entry.name for entry in selection.recipes if selected.intersection(entry.selectors)]
            report["observations"] = get_cooking_stock(detail.task_id)
            report["queue_completed"] = bool(detail.status.succeeded and len(report["visited"]) == len(selection.recipes))
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
