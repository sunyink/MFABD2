"""出售批次的记录、柜台内查找与续批；点击出售只由 Pipeline 执行。"""

import json
from copy import deepcopy
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from utils import mfaalog, arbitrage_store as store
from utils.account_sync import sync_from_context
from utils.persistent_store import PersistentStore
from utils.arbitrage_sale_state import SaleBatch, get_batch, put_batch, take_batch
from . import gold_verify, arbitrage_sell_quantity as quantity


_QUANTITY = "Arbitrage_Sell_Item_Quantity"
_LIST = "Arbitrage_Sell_Item_ListTraverse"
_EXIT = "Arbitrage_Sell_Item_BatchExit"
_END = "Arbitrage_Sell_Item_BatchEnd"
_FAILED = "Arbitrage_Sell_Item_BatchExitFailed"
_SWIPE = "Arbitrage_ItemList_Swip"


def check_scope(context, batch):
    if context.tasker.stopping:
        raise RuntimeError("出售任务已停止")
    if not sync_from_context(context, where="sale_batch"):
        raise RuntimeError("出售账号不可确认")
    if PersistentStore._current_account_id != batch.account_id:
        raise RuntimeError("出售期间账号已改变")
    if store.market_day() != batch.day:
        raise RuntimeError("出售期间游戏日已刷新，停止旧日计划")


def _batch(argv):
    raw = argv.custom_action_param
    params = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    return get_batch(params.get("request_id"))


def gold_proof(detail):
    """从本次识别证据取原值，不在确认动作中再次截图读金币。"""
    raw = getattr(detail, "raw_detail", None)
    if not isinstance(raw, dict):
        raise ValueError("金币识别缺少原始证据")
    pending = [raw]
    for _ in range(32):
        if not pending:
            break
        value = pending.pop()
        if isinstance(value, dict):
            if all(key in value for key in ("before", "after", "delta", "direction")):
                return {key: value[key] for key in ("before", "after", "delta")}
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    raise ValueError("金币识别证据没有本批差额")


@AgentServer.custom_action("ArbitrageSaleConfirmed")
class ArbitrageSaleConfirmed(CustomAction):
    def run(self, context, argv):
        batch = None
        try:
            batch = _batch(argv)
            check_scope(context, batch)
            batch.confirm(gold_proof(argv.reco_detail))
            mfaalog.info(f"[Arbitrage] [{batch.request['item_name']}] 本批报价核对通过，"
                         f"确认卖出{batch.actual_quantity}个，剩余{batch.inventory_after}个")
            return True
        except Exception as exc:
            if batch is not None:
                batch.fail(exc)
            mfaalog.warning(f"[Arbitrage] 出售金额确认异常：{exc}")
            return False


@AgentServer.custom_action("ArbitrageSaleMismatch")
class ArbitrageSaleMismatch(CustomAction):
    def run(self, context, argv):
        batch = None
        try:
            batch = _batch(argv)
            check_scope(context, batch)
            if not batch.retry():
                mfaalog.warning(f"[Arbitrage] [{batch.request['item_name']}] {batch.reason}，"
                                "成交数量未知，停止该物品")
                return False
            # 回读原库存不再依赖旧金币基准；如需补卖，开窗前会记录新基准。
            gold_verify.clear_verdict()
            quantity.take_result(batch.request["request_id"])
            mfaalog.warning(f"[Arbitrage] [{batch.request['item_name']}] 金币差额与报价未核对，"
                            "切购买→出售回顶，只核对并补试一次")
            return True
        except Exception as exc:
            if batch is not None:
                batch.fail(exc)
            mfaalog.warning(f"[Arbitrage] 出售补试未执行：{exc}")
            return False


@AgentServer.custom_action("ArbitrageSaleSearch")
class ArbitrageSaleSearch(CustomAction):
    """只从已确认回顶的出售列表进入；先查目标，再判类别末端。"""

    def run(self, context, argv):
        batch = None
        try:
            batch = _batch(argv)
            config = context.get_node_object(argv.node_name).attach
            max_pages = config["max_pages"]
            if type(max_pages) is not int or not 1 <= max_pages <= 32:
                raise ValueError("出售查找页数配置无效")
            for page in range(max_pages):
                check_scope(context, batch)
                image = context.tasker.controller.post_screencap().wait().get()
                if image is None or not image.size:
                    raise RuntimeError("出售列表截图失败")
                ready = context.run_recognition("Agt_SellList_Ready", image)
                if ready is None or not ready.hit:
                    raise RuntimeError("未确认当前柜台出售列表")
                found = context.run_recognition(_LIST, image)
                if found is None:
                    raise RuntimeError("出售目标识别未执行")
                if found.hit:
                    return True
                end_node = (context.get_anchor("Arbitrage_Sell_ListEnd")
                            or "Arbitrage_Sell_Item_ListTraverse_End")
                end = context.run_recognition(end_node, image)
                if end is None:
                    raise RuntimeError("出售列表末端识别未执行")
                if end.hit:
                    batch.absent()
                    mfaalog.info(f"[Arbitrage] [{batch.request['item_name']}] 从顶部查至类别末端，库存为0")
                    return False
                if page + 1 < max_pages:
                    moved = context.run_task(_SWIPE)
                    if moved is None or not moved.status.succeeded or not any(
                        node.name == _SWIPE and node.action is not None and node.action.success
                        for node in moved.nodes
                    ):
                        raise RuntimeError("出售列表下滑未完成，不能判零")
            raise RuntimeError("出售列表查找达到页数上限，不能判零")
        except Exception as exc:
            if batch is not None:
                batch.fail(exc)
            mfaalog.warning(f"[Arbitrage] 柜台查找失败：{exc}")
            return False


def _overrides(base, request):
    result = deepcopy(base)
    result.update({
        _QUANTITY: {"custom_action_param": request, "on_error": [_EXIT]},
        "Arbitrage_Sell_HUB": {"anchor": {"Sell_Bypass": ""}},
        "Arbitrage_Sell_PackShopSwich_PostOcr": {"next": [
            "[JumpBack]Arbitrage_ItemList_Sorting_Entry", "Arbitrage_Sell_Item_ListReset"]},
        "Arbitrage_Sell_Item_Click": {"on_error": [_EXIT]},
        "Arbitrage_Sell_Item_SellMenu": {"on_error": [_EXIT]},
        _LIST: {**result.get(_LIST, {}), "timeout": 4000, "on_error": [_EXIT]},
    })
    for name in ("Arbitrage_Sell_Item_AmountConfirmed", "Arbitrage_Sell_Item_AmountMismatch",
                 "Arbitrage_Sell_Item_Search"):
        result.setdefault(name, {})["custom_action_param"] = {"request_id": request["request_id"]}
    # 未经本批选量注入前，金额识别绝不能沿用上一批的期望值。
    result["Arbitrage_Sell_Item_AmountConfirmed"]["custom_recognition_param"] = {
        "direction": "increase", "expected_delta": 0}
    return result


def execute_sale_item(context, item_name, base_override, *, reserve=0, target=None):
    """执行同一物品的有限计划；每批一次补试由 Pipeline 旁路承担。"""
    quantity.plan_quantity(0, reserve, target)
    if reserve == -1 or target == 0:
        return {"status": "skipped", "actual_quantity": 0, "batches": [], "page_ok": True}
    if not sync_from_context(context, where="execute_sale_item"):
        raise RuntimeError("出售账号不可确认")
    account_id, day = PersistentStore._current_account_id, store.market_day()
    local = context.clone()
    remaining = target
    expected_inventory = None
    total = 0
    rows = []
    entry = "Arbitrage_Sell_HUB"
    while remaining is None or remaining > 0:
        request = {"request_id": uuid4().hex, "item_name": item_name,
                   "reserve": reserve, "target": remaining, "managed_sale": True}
        if expected_inventory is not None:
            request["expected_inventory"] = expected_inventory
        batch = SaleBatch(request, account_id, day)
        put_batch(batch)
        detail = None
        try:
            check_scope(local, batch)
            for name in (_SWIPE, "Arbitrage_Sell_Item_Cancel", "Arbitrage_Sell_Item_Exit_ResetSwip"):
                if not local.clear_hit_count(name):
                    raise RuntimeError(f"无法重置出售节点计数：{name}")
            gold_verify.clear_verdict()
            detail = local.run_task(entry, pipeline_override=_overrides(base_override, request))
            if batch.status not in ("confirmed", "skipped", "rejected", "unknown"):
                batch.fail("出售链未取得本批确认结果")
        except Exception as exc:
            batch.fail(exc)
            mfaalog.warning(f"[Arbitrage] [{item_name}] 本批未完成：{exc}")
        finally:
            quantity.take_result(request["request_id"])
            take_batch(request["request_id"])
            gold_verify.clear_verdict()
        page_ok = detail is not None and detail.status.succeeded and any(
            node.name == _END and node.completed for node in detail.nodes
        ) and not any(node.name == _FAILED for node in detail.nodes)
        row = batch.result()
        rows.append(row)
        total += row["actual_quantity"]
        # 交易记录属于原账号；账号变化时停止，不能把它写进新账号。
        if not sync_from_context(context, where="sale_result") or PersistentStore._current_account_id != account_id:
            return {"status": "unknown", "actual_quantity": total, "batches": rows, "page_ok": False}
        try:
            if row["uncertain"]:
                saved = store.invalidate_inventory_quantities([item_name], "sale_unconfirmed", reference=row)
            elif row["inventory_after"] is not None:
                saved = store.set_inventory_quantities({item_name: row["inventory_after"]},
                                                        "sale_confirmed", reference=row)
            else:
                saved = True
            if not saved:
                raise RuntimeError("出售后库存状态保存失败")
        except Exception as exc:
            try:
                if not store.invalidate_inventory_quantities([item_name], "sale_save_failed"):
                    mfaalog.error(f"[Arbitrage] [{item_name}] 旧库存也未能作废，需重新仓检")
            except Exception as invalidate_exc:
                mfaalog.error(f"[Arbitrage] [{item_name}] 旧库存作废异常：{invalidate_exc}")
            mfaalog.error(f"[Arbitrage] [{item_name}] {exc}，停止出售")
            return {"status": "unknown", "actual_quantity": total, "batches": rows, "page_ok": False}
        if not page_ok or row["status"] != "confirmed":
            return {"status": row["status"], "actual_quantity": total, "batches": rows, "page_ok": page_ok}
        if row["actual_quantity"] <= 0 or row["inventory_after"] is None:
            raise RuntimeError("出售已确认却没有有效成交数量或库存")
        expected_inventory = row["inventory_after"]
        # 首批确认后固定本物品剩余目标；不因后续重新开窗而扩张计划。
        remaining = max(expected_inventory - reserve, 0) if remaining is None else remaining - row["actual_quantity"]
        remaining = min(remaining, max(expected_inventory - reserve, 0))
        entry = "Arbitrage_Sell_Item_Resume"
    return {"status": "confirmed", "actual_quantity": total, "batches": rows, "page_ok": True}
