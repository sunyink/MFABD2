"""仓检后的单次补充阶段：计划、补买、必要补做；最终出售仍由原Hub派发。"""

from collections import Counter, OrderedDict
from copy import deepcopy
import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .arbitrage_buy_precise import parse_money
from .arbitrage_replenish_buy import ensure_shop, execute_replenish_purchases
from .arbitrage_replenish_cook import execute_replenish_cooking
from .arbitrage_result import ArbitrageSellController
from .bag_stock import get_bag_scan_run
from .shop_buy_fav_controller import ShopBuyFavController, DATA_NODE
from utils import arbitrage_store as store, mfaalog
from utils.name_i18n import canon
from utils.account_sync import sync_from_context
from utils.arbitrage_recipe_catalog import discover_recipe_entries
from utils.arbitrage_replenish_data import load_replenish_data
from utils.arbitrage_replenish_plan import build_replenish_plan


_RESULTS = OrderedDict()


def _summary(report):
    purchases = report.get("purchases", {})
    cooking = report.get("cooking", {})
    counts = Counter(row["result"].get("status") for row in purchases.get("results", []))
    skipped = sum(counts[name] for name in ("skipped", "not_found", "rejected", "stale"))
    status = {"completed": "本轮完成", "partial": "本轮结束", "prepared": "仅调数结束",
              "skipped": "本轮跳过", "stopped": "本轮停止"}.get(report["status"], "本轮结束")
    parts = [f"[Replenish] {status}"]
    if report.get("reason"):
        parts.append(report["reason"])
    if purchases.get("results"):
        parts.append(f"购买成功{counts['confirmed']}笔，部分买入{counts['partial']}笔，跳过{skipped}笔")
    if purchases.get("pending_requests"):
        parts.append(f"未执行{len(purchases['pending_requests'])}笔")
    parts.append(f"已确认支出={purchases.get('confirmed_spend', 0)}金币")
    if purchases.get("unconfirmed_count"):
        parts.append(f"另有{purchases['unconfirmed_count']}笔未核对成功，总支出未完全确认")
    planned = report.get("plan", {}).get("cook_today_candidates", [])
    if not planned:
        parts.append("计划无补做" if "plan" in report else "未安排补做")
    elif cooking.get("started"):
        parts.append(f"计划补做{len(planned)}项，实际选择{len(cooking.get('selected', []))}项")
    else:
        parts.append(f"计划补做{len(planned)}项，本轮未启动")
    if cooking.get("reason"):
        parts.append(f"补做说明：{cooking['reason']}")
    return "；".join(parts)


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


def _supplemental_supply(context, data):
    """常规收藏表负责的柜台商品不再安排补买；配置排除不代表实测售罄。"""
    node = context.get_node_object(DATA_NODE)
    attach = getattr(node, "attach", None)
    if not isinstance(attach, dict):
        raise ValueError("常规购买表不可读，不能确定补买范围")
    regular = {}
    parser = ShopBuyFavController()
    for key, raw in attach.items():
        if key == "ocr_exclude":
            continue
        if not isinstance(key, str) or ":" not in key or not isinstance(raw, str):
            raise ValueError(f"常规购买表格式无效: {key}")
        shop = canon(key.split(":", 1)[1].strip())
        regular.setdefault(shop, set()).update(parser._parse_item_list(raw))
    filtered = deepcopy(data)
    excluded = []
    for shop_name, shop in filtered["shops"].items():
        for name in list(shop["items"]):
            if canon(name) in regular.get(canon(shop_name), set()):
                del shop["items"][name]
                excluded.append({"shop_name": shop_name, "item_name": name})
    return filtered, excluded


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
    supply, excluded = _supplemental_supply(context, data)
    report["regular_purchase_exclusions"] = excluded
    sell_names = (ArbitrageSellController._load_whitelist(context)
                  if _enabled(context, "Arbitrage_SellItem") else set())
    # 仅用有限供给总额筛是否有需求；有请求后才进店，以实读金币重算正式预算。
    ceiling = sum(item["price_reference"] * min(item["daily_limit_reference"], 99999)
                  for shop in supply["shops"].values() for item in shop["items"].values())
    plan = build_replenish_plan(entries, inventory, market, supply, day=day,
                                budget=ceiling if budget is None else budget, sell_names=sell_names)
    report.update(day=day, bag_run_id=bag_run_id, plan=plan)
    if not plan["requests"]:
        mfaalog.info("[Replenish] 本轮无需补买")
        return {**report, "reason": "没有符合条件的补买需求"}
    ensure_shop(context)
    available_gold = _gold(context)
    if store.market_day() != day:
        return {**report, "reason": "日期已刷新，旧日计划取消"}
    inventory = store.get_replenish_inventory(bag_run_id)["quantities"]
    plan = build_replenish_plan(entries, inventory, market, supply, day=day,
                                budget=available_gold if budget is None else min(budget, available_gold),
                                sell_names=sell_names)
    report["plan"] = plan
    planned_cooking = tuple(plan["cook_today_candidates"])
    if plan["requests"]:
        mfaalog.info("[Replenish] 本轮补买计划（数量与金额为预计值）：\n" + "\n".join(
            f"[{row['shop_name']}] {row['item_name']} × {row['target']}，预计{row['budget']}金币"
            for row in plan["requests"]) + f"\n预计合计{plan['estimated_spend']}金币，本轮预算{plan['budget']}金币；"
            + "计划补做：" + ("、".join(planned_cooking) or "无"))
    else:
        mfaalog.info("[Replenish] 本轮无需补买：当前金币预算下没有可执行采购")
    bought = execute_replenish_purchases(context, plan, bag_run_id=bag_run_id, dry_run=dry_run)
    report["purchases"] = bought
    if bought["status"] == "expired":
        return {**report, "status": "skipped", "reason": "日期已刷新，取消余下旧日补买与补做"}
    if bought["status"] not in ("completed", "partial", "prepared", "empty"):
        return {**report, "status": "stopped", "return_ok": False, "reason": "补买结果未完整确认"}
    if dry_run:
        return {**report, "status": "prepared", "reason": "只调数已结束，未购买或补做"}
    cooked = execute_replenish_cooking(context, planned_cooking, day=day,
                                       bag_run_id=bag_run_id, callback_task_id=task_id)
    report["cooking"] = cooked
    if cooked["status"] == "stopped" or (cooked.get("started") and not cooked.get("returned")):
        return {**report, "status": "stopped", "return_ok": False, "reason": "补做收尾未确认"}
    report["status"] = "partial" if bought["status"] == "partial" or cooked["status"] == "incomplete" else "completed"
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
        text = _summary(report)
        (mfaalog.info if report["return_ok"] else mfaalog.warning)(text)
        return report["return_ok"]


@AgentServer.custom_action("ArbitragePrepareFinalSale")
class ArbitragePrepareFinalSale(CustomAction):
    def run(self, context, argv):
        try:
            # 仓检留在主页、补做留在料理菜单；由出售自己恢复页面，空补买计划无需进店。
            ensure_shop(context, bargain=False)
            return True
        except Exception as exc:
            mfaalog.warning(f"[Replenish] 最终出售准备失败，本任务停止：{exc}")
            return False
