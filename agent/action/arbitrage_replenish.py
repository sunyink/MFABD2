"""仓检后的单次补充阶段：计划、补买、必要补做；最终出售仍由原Hub派发。"""

from collections import OrderedDict
from copy import deepcopy
import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .arbitrage_buy_precise import parse_money
from .arbitrage_replenish_buy import ensure_shop, execute_replenish_purchases
from .arbitrage_replenish_cook import execute_replenish_cooking
from .arbitrage_result import ArbitrageSellController
from .bag_stock import get_bag_scan_run
from utils import arbitrage_store as store, mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_recipe_catalog import discover_recipe_entries
from utils.arbitrage_replenish_data import load_replenish_data
from utils.arbitrage_replenish_plan import build_replenish_plan


_RESULTS = OrderedDict()


def get_replenish_result(task_id):
    """只返回本进程最近任务的回报副本，不从旧存档恢复交易指令。"""
    return deepcopy(_RESULTS.get(task_id))


def _enabled(context, name):
    node = context.get_node_data(name)
    if not isinstance(node, dict):
        raise ValueError(f"补充阶段所需节点不可读: {name}")
    return node.get("enabled", True) and node.get("max_hit", 2 ** 32 - 1) != 0


def _gold(context):
    """已确认商店列表后连续两帧核对金币；不使用旧存档金币或倍率。"""
    values = []
    for _ in range(2):
        if context.tasker.stopping:
            raise RuntimeError("任务已停止")
        image = context.tasker.controller.post_screencap().wait().get()
        if image is None or not image.size:
            raise RuntimeError("补买预算截图失败")
        result = context.run_recognition("Agt_BuyQuantity_Gold_Ocr", image)
        if result is None or not result.hit:
            raise RuntimeError("补买预算金币未读清")
        texts = [item.get("text", "") if isinstance(item, dict) else item.text
                 for item in result.filtered_results]
        values.append(parse_money(texts))
    if values[0] != values[1]:
        raise RuntimeError("补买预算金币读数仍在变化")
    return values[0]


def run_replenishment(context, task_id, config):
    """return_ok供外层Custom决定是否进入专用StopTask；内部不调用自身。"""
    report = {"status": "skipped", "return_ok": True, "task_id": task_id}
    if not isinstance(config, dict):
        raise ValueError("补充主控参数必须为对象")
    budget = config.get("budget")
    dry_run = config.get("dry_run", False)
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("补买预算必须为非负整数或null（使用当前金币）")
    if type(dry_run) is not bool:
        raise ValueError("dry_run必须为布尔值")
    if context.tasker.stopping:
        return {**report, "status": "stopped", "return_ok": False, "reason": "task_stopping"}
    if not _enabled(context, "Arbitrage_BuyItem"):
        return {**report, "reason": "购买已关闭"}
    if not any(_enabled(context, name) for name in ("Arbitrage_Cooking", "Arbitrage_Cooking_Weekly")):
        return {**report, "reason": "料理制作已关闭"}
    if not sync_from_context(context, where="ArbitrageReplenishController"):
        return {**report, "status": "stopped", "return_ok": False, "reason": "账号同步失败"}
    bag_run_id = get_bag_scan_run(task_id)
    if not bag_run_id:
        return {**report, "reason": "没有本任务仓检记录"}
    inventory = store.get_replenish_inventory(bag_run_id)["quantities"]
    day = store.market_day()
    market = store.get_market_snapshot(day)
    if market is None:
        return {**report, "reason": "今日完整行情不可用"}
    entries = discover_recipe_entries(context)
    data = load_replenish_data()
    sell_names = (ArbitrageSellController._load_whitelist(context)
                  if _enabled(context, "Arbitrage_SellItem") else set())
    # 仅用有限供给总额筛是否有需求；有请求后才进店，以实读金币重算正式预算。
    ceiling = sum(item["price_reference"] * min(item["daily_limit_reference"], 99999)
                  for shop in data["shops"].values() for item in shop["items"].values())
    plan = build_replenish_plan(entries, inventory, market, data, day=day,
                                budget=ceiling if budget is None else budget, sell_names=sell_names)
    report.update(day=day, bag_run_id=bag_run_id, plan=plan)
    if not plan["requests"]:
        return {**report, "reason": "没有符合条件的补买需求"}
    ensure_shop(context)
    available_gold = _gold(context)
    if store.market_day() != day:
        return {**report, "reason": "日期已刷新，旧日计划取消"}
    inventory = store.get_replenish_inventory(bag_run_id)["quantities"]
    plan = build_replenish_plan(entries, inventory, market, data, day=day,
                                budget=available_gold if budget is None else min(budget, available_gold),
                                sell_names=sell_names)
    report["plan"] = plan
    bought = execute_replenish_purchases(context, plan, bag_run_id=bag_run_id, dry_run=dry_run)
    report["purchases"] = bought
    if bought["status"] not in ("completed", "prepared", "empty"):
        return {**report, "status": "stopped", "return_ok": False, "reason": "补买结果未完整确认"}
    if dry_run:
        return {**report, "status": "prepared", "reason": "只调数已结束，未购买或补做"}
    cooked = execute_replenish_cooking(context, bought, store.get_market_snapshot(day), data,
                                       bag_run_id=bag_run_id, sell_names=sell_names)
    report["cooking"] = cooked
    if cooked["status"] == "stopped" or (cooked.get("started") and not cooked.get("returned")):
        return {**report, "status": "stopped", "return_ok": False, "reason": "补做收尾未确认"}
    report["status"] = "partial" if cooked["status"] == "incomplete" else "completed"
    return report


@AgentServer.custom_action("ArbitrageReplenishController")
class ArbitrageReplenishController(CustomAction):
    def run(self, context, argv):
        task_id = argv.task_detail.task_id
        try:
            raw = argv.custom_action_param
            config = raw if isinstance(raw, dict) else json.loads(str(raw))
            report = run_replenishment(context, task_id, config)
        except Exception as exc:
            report = {"status": "stopped", "return_ok": False, "reason": str(exc), "task_id": task_id}
        _RESULTS[task_id] = report
        _RESULTS.move_to_end(task_id)
        while len(_RESULTS) > 16:
            _RESULTS.popitem(last=False)
        purchases = report.get("purchases", {})
        cooking = report.get("cooking", {})
        text = (f"[Replenish] {report['status']}：{report.get('reason', '本轮完成')}；"
                f"确认支出={purchases.get('confirmed_spend', 0)}，"
                f"补做选择={len(cooking.get('selected', []))}项")
        (mfaalog.info if report["return_ok"] else mfaalog.warning)(text)
        return report["return_ok"]


@AgentServer.custom_action("ArbitragePrepareFinalSale")
class ArbitragePrepareFinalSale(CustomAction):
    def run(self, context, argv):
        try:
            # 仓检留在主页、补做留在料理菜单；由出售自己恢复页面，空补买计划无需进店。
            ensure_shop(context)
            if not context.clear_hit_count("Arbitrage_Sell_PriceList_FirstCalibration"):
                raise RuntimeError("最终出售价目表校准计数未能重置")
            return True
        except Exception as exc:
            mfaalog.warning(f"[Replenish] 最终出售准备失败，本任务停止：{exc}")
            return False
