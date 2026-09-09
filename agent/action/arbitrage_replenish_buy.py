"""补买批次执行：显式计划、当前仓检编号、实际支出与绝对库存；不负责补做。"""

from copy import deepcopy

from .arbitrage_buy_precise import execute_buy
from utils.account_sync import sync_from_context
from utils import arbitrage_store as store, mfaalog
from utils.name_i18n import canon


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label}必须为大于等于{minimum}的整数")
    return value


def _requests(plan):
    if not isinstance(plan, dict) or plan.get("status") != "planned" or plan.get("day") != store.market_day():
        raise ValueError("补买计划不是当前日的有效计划")
    _integer(plan.get("budget"), "本轮预算")
    if not isinstance(plan.get("requests"), list) or not isinstance(plan.get("input_quantities"), dict):
        raise ValueError("补买计划缺少请求或输入库存")
    for name, value in plan["input_quantities"].items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("计划库存名称无效")
        _integer(value, "计划库存")
    merged = {}
    for row in plan["requests"]:
        if not isinstance(row, dict):
            raise ValueError("补买请求不是对象")
        for field in ("shop_name", "item_name"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"补买请求缺少{field}")
        name = canon(row["item_name"].strip())
        shop = row["shop_name"].strip()
        target = _integer(row.get("target"), "目标量")
        budget = _integer(row.get("budget"), "请求预算")
        price = _integer(row.get("max_unit_price"), "单价上限", 1)
        if name not in plan["input_quantities"]:
            raise ValueError(f"补买材料没有已确认的计划库存: {name}")
        if not target or not budget:
            continue
        key = (shop, name)
        if key not in merged:
            merged[key] = {"shop_name": shop, "item_name": name, "target": 0, "budget": 0,
                           "max_unit_price": price, "uses": []}
        current = merged[key]
        if current["max_unit_price"] != price:
            raise ValueError(f"同柜台同材料出现不同单价上限: {key}")
        current["target"] += target
        current["budget"] += budget
        if current["target"] > 99999:
            raise ValueError("合并请求超过单批上限99999")
        uses = row.get("uses", [])
        if not isinstance(uses, list):
            raise ValueError("补买用途必须为列表")
        for use in uses:
            if not isinstance(use, dict) or not isinstance(use.get("recipe"), str) or not use["recipe"]:
                raise ValueError("补买用途缺少菜谱")
            _integer(use.get("quantity"), "用途数量", 1)
        if uses and sum(use["quantity"] for use in uses) != target:
            raise ValueError("补买用途合计与目标量不同")
        current["uses"].extend(deepcopy(uses))
    if sum(row["budget"] for row in merged.values()) > plan["budget"]:
        raise ValueError("请求累计预算超过本轮预算")
    return list(merged.values())


def ensure_shop(context):
    """只在局部改写原寻路终点；不能重新进入Action_Hub或固定收藏购买。"""
    if context.tasker.stopping:
        raise RuntimeError("任务已停止")
    local = context.clone()
    if not local.clear_hit_count("Arbitrage_Sell_Item_Cancel"):
        raise RuntimeError("无法重置商店子页关闭计数")
    patch = {
        "Arbitrage_Replenish_EnsureShop": {"enabled": True, "anchor": {"PractiseBargaining_Per": ""}},
        "Arbitrage_Merchant_Ico": {"next": ["Arbitrage_Merchant_Entry"]},
        "Arbitrage_Merchant_Egress": {"next": ["Arbitrage_Replenish_ShopReady"]},
        "Arbitrage_Action_Hub": {"enabled": False},
    }
    result = local.run_task("Arbitrage_Replenish_EnsureShop", patch)
    if (result is None or not result.status.succeeded
            or not any(node.name == "Arbitrage_Replenish_ShopReady" for node in result.nodes)):
        raise RuntimeError("未确认商店列表，停止本轮补买")


def _receipt(result, request):
    """执行器已经核对三项差额；消费前再验证类型及预算边界，不推测成交量。"""
    amount = _integer(result.get("actual_quantity"), "实买量", 1)
    spent = _integer(result.get("actual_spent"), "实付金额", 1)
    before = _integer(result.get("owned"), "购前拥有量")
    after = _integer(result.get("owned_after"), "购后拥有量")
    unit = _integer(result.get("unit_price"), "实际单价", 1)
    available = _integer(result.get("available"), "购前店余")
    remaining = _integer(result.get("remaining_stock"), "购后店余")
    gold_before = _integer(result.get("gold"), "购前金币")
    gold_after = _integer(result.get("gold_after"), "购后金币")
    if (amount > request["target"] or spent > request["budget"] or unit > request["max_unit_price"]
            or before != request["expected_owned"] or after - before != amount
            or available - remaining != amount or gold_before - gold_after != spent or amount * unit != spent):
        raise ValueError("补买回报与库存、店余、金币或预算不一致")
    return amount, spent, after


def execute_replenish_purchases(context, plan, *, bag_run_id, dry_run=True):
    """P4入口；bag_run_id必须由当前任务get_bag_scan_run取得，不能从旧存档猜。

    未知成交后remaining_budget=None，停止余下请求并作废受影响材料旧量。
    返回的purchased只累计已确认收据；补做须用最新有效库存重新筛，不读预测余额。
    """
    if type(dry_run) is not bool:
        raise ValueError("dry_run必须为布尔值")
    requests = _requests(plan)
    report = {"status": "empty", "day": plan["day"], "bag_run_id": bag_run_id,
              "budget": plan["budget"], "confirmed_spend": 0,
              "remaining_budget": plan["budget"], "purchased": {}, "inventory_after": {}, "results": [],
              "unfilled_uses": [], "shop_observations": [], "pending_requests": deepcopy(requests)}
    if not requests:
        return report
    if not sync_from_context(context, where="ArbitrageReplenishBuy"):
        report.update(status="stopped", reason="account_sync_failed")
        return report
    try:
        current = store.get_replenish_inventory(bag_run_id)["quantities"]
    except Exception as exc:
        report.update(status="stopped", reason="inventory_context_changed", inventory_error=str(exc))
        return report
    changed = [name for name, value in plan["input_quantities"].items() if current.get(name) != value]
    if changed:
        report.update(status="stale_plan", changed_items=changed)
        return report
    try:
        ensure_shop(context)
    except Exception as exc:
        report.update(status="stopped", reason=str(exc))
        return report
    for index, original in enumerate(requests):
        if context.tasker.stopping:
            report.update(status="stopped", reason="task_stopping")
            return report
        try:
            latest = store.get_replenish_inventory(bag_run_id)["quantities"]
            changed = [name for name, value in current.items() if latest.get(name) != value]
            if changed:
                report.update(status="stale_plan", changed_items=changed)
                return report
        except Exception as exc:
            report.update(status="stopped", reason="inventory_context_changed", inventory_error=str(exc))
            return report
        request = {**original, "budget": min(original["budget"], report["remaining_budget"]),
                   "expected_owned": current[original["item_name"]], "dry_run": dry_run}
        if not request["budget"]:
            report.update(status="stopped", reason="budget_exhausted")
            return report
        mfaalog.info(f"[ReplenishBuy] {index + 1}/{len(requests)} [{request['shop_name']}] "
                     f"{request['item_name']}最多{request['target']}个，预算{request['budget']}")
        try:
            result = execute_buy(context, request)
            if not isinstance(result, dict):
                raise ValueError("补买执行器没有返回结构化结果")
        except Exception as exc:
            result = {"status": "unknown", "reason": str(exc)}
        status = result.get("status")
        amount = spent = 0
        if status in ("prepared", "skipped", "not_found", "rejected", "stale") and any(
            type(result.get(key)) is not int or result[key] != 0 for key in ("actual_quantity", "actual_spent")
        ):
            result = {"status": "unknown", "reason": "未交易状态没有明确的零成交回报"}
            status = "unknown"
        if not dry_run and status in ("confirmed", "partial"):
            try:
                amount, spent, after = _receipt(result, request)
            except (ValueError, TypeError) as exc:
                result = {"status": "unknown", "reason": str(exc)}
                status = "unknown"
        allowed = {"prepared", "skipped", "not_found", "rejected", "stale"} if dry_run else {
            "confirmed", "partial", "skipped", "not_found", "rejected", "stale"}
        if status not in allowed:
            report["results"].append({"request": deepcopy(request), "result": result})
            report.update(status="stopped", reason="purchase_unconfirmed", remaining_budget=None,
                          pending_requests=deepcopy(requests[index + 1:]))
            if not dry_run:
                try:
                    store.get_replenish_inventory(bag_run_id)
                    invalidated = store.invalidate_inventory_quantities(
                        [request["item_name"]], "replenish_purchase_unknown",
                        {"shop_name": request["shop_name"], "result": result})
                    report["inventory_invalidated"] = invalidated
                except Exception as exc:
                    report["inventory_error"] = str(exc)
                report["inventory_after"].pop(request["item_name"], None)
            return report
        report["results"].append({"request": deepcopy(request), "result": result})
        report["pending_requests"] = deepcopy(requests[index + 1:])
        remaining_stock = result.get("remaining_stock") if amount else result.get("available")
        unit_price = result.get("unit_price")
        if type(remaining_stock) is int and remaining_stock >= 0 and type(unit_price) is int and unit_price > 0:
            report["shop_observations"].append({"day": plan["day"], "shop_name": request["shop_name"],
                                                "item_name": request["item_name"], "remaining": remaining_stock,
                                                "unit_price": unit_price})
        if status == "stale":
            report.update(status="stale_plan", changed_items=[request["item_name"]])
            return report
        if amount:
            report["confirmed_spend"] += spent
            report["remaining_budget"] -= spent
            name = request["item_name"]
            report["purchased"][name] = report["purchased"].get(name, 0) + amount
            try:
                # 再核对账号仍持有同一个仓检标识，避免向另一账号写入本批收据。
                store.get_replenish_inventory(bag_run_id)
                if not store.set_inventory_quantities({request["item_name"]: after}, "replenish_purchase", {
                    "bag_run_id": bag_run_id, "shop_name": request["shop_name"],
                    "actual_quantity": amount, "actual_spent": spent}):
                    raise RuntimeError("确认购买后的库存写入失败")
            except Exception as exc:
                report.update(status="stopped", reason="inventory_write_failed", inventory_error=str(exc))
                report["inventory_after"].pop(name, None)
                try:
                    store.get_replenish_inventory(bag_run_id)
                    report["inventory_invalidated"] = store.invalidate_inventory_quantities(
                        [request["item_name"]], "replenish_purchase_write_failed")
                except Exception:
                    report["inventory_invalidated"] = False
                return report
            current[name] = after
            report["inventory_after"][name] = after
        if not dry_run:
            remaining = amount
            for use in original["uses"]:
                assigned = min(remaining, use["quantity"])
                remaining -= assigned
                if assigned < use["quantity"]:
                    report["unfilled_uses"].append({"recipe": use["recipe"], "item_name": original["item_name"],
                                                   "quantity": use["quantity"] - assigned})
    report["status"] = "prepared" if dry_run else "completed"
    return report
