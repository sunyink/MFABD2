"""按显式请求复用出售HUB补买；共用定位与调数，购买条件和结果单独核对。"""

import json
import re
import time
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .arbitrage_sell_quantity import QuantityAdjuster, parse_quantity, _clean, _integer, _COUNT
from .gold_verify import clear_verdict, get_baseline
from utils import mfaalog
from utils.arbitrage_quote import parse_money, parse_quote as parse_cost


_QUANTITY = "Arbitrage_Sell_Item_Quantity"
_EXIT = "Arbitrage_Sell_Item_Exit"
_RESULTS = {}
_BUY_BUTTON = r"^\s*[购購][买買]\s*$"
_CONFIRM = "Arbitrage_Sell_Item_Selling"


def take_buy_result(request_id):
    """按调用者提供的request_id领取结果；选量/报价不等于实际买入/支出。"""
    return _RESULTS.pop(request_id, None)


def parse_available(texts):
    values = set()
    for text in texts:
        match = re.fullmatch(r"可[购購][买買](" + _COUNT + r")[个個]", _clean(text))
        if match:
            values.add(int(match[1].replace(",", "")))
    if len(values) != 1:
        raise ValueError(f"商店可买余量没有唯一完整读数: {texts!r}")
    return values.pop()


def purchase_limit(target, available, unit_price, gold, budget, max_unit_price):
    for label, value in (("目标量", target), ("商店余量", available), ("金币", gold),
                         ("预算", budget), ("单价上限", max_unit_price)):
        _integer(value, label)
    _integer(unit_price, "实际单价", 1)
    if unit_price > max_unit_price:
        return 0
    return min(target, available, 99999, gold // unit_price, budget // unit_price)


class BuyQuantityAdjuster(QuantityAdjuster):
    def __init__(self, context, config, request):
        super().__init__(context, config, {**request, "reserve": 0})
        for key in ("target", "max_unit_price", "budget"):
            _integer(request.get(key), key)
        if "expected_owned" in request:
            _integer(request["expected_owned"], "计划时库存")

    def state(self, image):
        owned = super().state(image)
        available = parse_available(self.texts("available_node", image))
        gold = parse_money(self.texts("gold_node", image))
        result = self.recognize("cost_node", image)
        if not result.hit:
            raise ValueError(f"未读到{self.config['cost_node']}")
        items = getattr(result, "filtered_results", None) or []
        quantity, cost = parse_cost(items)
        selected = parse_quantity(self.texts("selected_node", image))
        if quantity != selected:
            raise ValueError(f"金额行数量{quantity}与独立选量{selected}不一致")
        return owned, available, gold, cost

    def prepare(self):
        self.check_running()
        initial, _ = self.read(full=True)
        owned, available, gold, _ = initial
        if "expected_owned" in self.request and owned != self.request["expected_owned"]:
            return {"status": "stale", "owned": owned, "available": available, "gold": gold,
                    "reason": "实际库存与计划输入不同，未购买"}
        if not self.request["target"] or not self.request["budget"] or not self.request["max_unit_price"]:
            return {"status": "skipped", "reason": "目标、预算或单价上限为0",
                    "owned": owned, "available": available, "gold": gold}
        self.action("min_node")
        minimum, selected = self.read(full=True)
        if selected != 1 or minimum[:3] != initial[:3]:
            raise RuntimeError("MIN未选中1件或购买条件改变")
        unit_price = minimum[3]
        target = purchase_limit(self.request["target"], available, unit_price, gold,
                                self.request["budget"], self.request["max_unit_price"])
        if not target:
            return {"status": "skipped", "reason": "价格超限、余量不足或预算不足",
                    "owned": owned, "available": available, "gold": gold, "unit_price": unit_price}
        # MAX可能还受持有金币限制，实际读上限，不能把背包拥有量用于购买选量。
        self.action("max_node")
        maximum = self.read()
        if not 1 <= maximum <= min(available, 99999):
            raise RuntimeError("购买MAX选量超出商店余量")
        target = min(target, maximum)
        self.adjust(target, maximum, max_selected=maximum)
        final, selected = self.read(full=True)
        if (final[:3] != initial[:3] or selected != target or final[3] != unit_price * selected
                or final[3] > min(gold, self.request["budget"])):
            raise RuntimeError("最终库存、商店余量、金币或购买总价核对失败")
        return {"status": "ready", "owned": owned, "available": available, "gold": gold,
                "unit_price": unit_price, "selected": selected, "quoted_total": final[3]}


@AgentServer.custom_action("ArbitrageBuyQuantity")
class ArbitrageBuyQuantity(CustomAction):
    def run(self, context, argv):
        request_id = None
        result = None
        try:
            raw = argv.custom_action_param
            request = raw if isinstance(raw, dict) else json.loads(str(raw))
            if not isinstance(request, dict):
                raise ValueError("补买请求必须为对象")
            request_id = request.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id):
                request_id = None
                raise ValueError("请求编号无效")
            result = BuyQuantityAdjuster(context, context.get_node_object(argv.node_name).attach, request).prepare()
            mfaalog.info(f"[PreciseBuy] [{request['item_name']}] {result}")
            return result["status"] == "ready"
        except Exception as exc:
            result = {"status": "rejected", "reason": str(exc)}
            mfaalog.warning(f"[PreciseBuy] 取消本批: {exc}")
            return False
        finally:
            if request_id is not None and result is not None:
                _RESULTS[request_id] = result


def confirm_purchase(context, request, config, ready):
    """Retry only an unchanged, fully checked purchase page; never replay a receipt."""
    if request.get("dry_run") or ready.get("status") != "ready":
        raise ValueError("本批没有可执行的真实购买选量")
    adjuster = BuyQuantityAdjuster(context, config, request)
    expected = (ready["owned"], ready["available"], ready["gold"], ready["quoted_total"])
    ready["confirmation_attempts"] = 0
    for attempt in range(3):
        # Two stable observations plus one final same-image state/button check before each click.
        image = adjuster.capture()
        button = context.run_recognition(_CONFIRM, image)
        if button is None:
            raise RuntimeError("购买按钮复检未执行")
        if not button.hit:
            return
        state, selected = adjuster.read(full=True)
        if state != expected or selected != ready["selected"]:
            mfaalog.info("[PreciseBuy] 购买页面数据已变化，停止点击，交给成交回读核对")
            return
        if attempt == 0:
            # 单节点任务提供Custom回调所需的识别上下文；局部切断后继，不进入商品点击链。
            snapshot = context.clone().run_task("Arbitrage_Sell_Gold_Snapshot", {
                "Arbitrage_Sell_Gold_Snapshot": {"recognition": "DirectHit", "action": "Custom",
                    "custom_action": "GoldSnapshot", "custom_action_param": {"node": config["gold_node"]},
                    "next": [], "on_error": []}})
            if snapshot is None or not snapshot.status.succeeded or get_baseline() != ready["gold"]:
                raise RuntimeError("购买金币基准与本批读数不一致，未点击")
        image = adjuster.capture()
        if (adjuster.state(image) != expected
                or parse_quantity(adjuster.texts("selected_node", image)) != ready["selected"]):
            return
        button = context.run_recognition(_CONFIRM, image)
        if button is None or not button.hit:
            return
        ready["confirmation_attempts"] = attempt + 1
        ready["purchase_attempted"] = True
        clicked = context.run_action("Agt_BuyConfirm_Click", box=button.box)
        mfaalog.info(f"[PreciseBuy] 购买确认点击 {attempt + 1}/3，等待2秒复检")
        time.sleep(2)
        if clicked is None or not clicked.success:
            raise RuntimeError("购买点击结果未知，停止重试")
    mfaalog.warning("[PreciseBuy] 购买确认已达3次，退出并核对实际成交；下批从0开始")


@AgentServer.custom_action("ArbitrageBuyConfirm")
class ArbitrageBuyConfirm(CustomAction):
    def run(self, context, argv):
        ready = None
        try:
            raw = argv.custom_action_param
            request = raw if isinstance(raw, dict) else json.loads(str(raw))
            ready = _RESULTS.get(request.get("request_id"))
            if not isinstance(ready, dict):
                raise ValueError("缺少本批已核对选量")
            confirm_purchase(context, request, context.get_node_object(_QUANTITY).attach, ready)
            return True
        except Exception as exc:
            if isinstance(ready, dict) and ready.get("purchase_attempted"):
                # 点击后窗口可能已经关闭；复读失败只结束重试，成交由金币核对决定。
                ready["confirmation_note"] = str(exc)
                mfaalog.info("[PreciseBuy] 已点击购买，结束重试并转金币核对")
                mfaalog.debug(f"[PreciseBuy] 购后重试结束原因：{exc}")
            else:
                if isinstance(ready, dict):
                    ready["reason"] = str(exc)
                mfaalog.warning(f"[PreciseBuy] 本批未点击购买：{exc}")
            return False


def buy_overrides(context, request):
    from .arbitrage_result import _sell_item_override, _cart_expected
    patch = _sell_item_override(context, request["item_name"])
    patch["Arbitrage_Sell_Item_ListTraverse"]["max_hit"] = 2
    config = dict(context.get_node_object(_QUANTITY).attach)
    config.update(available_node="Agt_BuyQuantity_Available_Ocr", gold_node="Agt_BuyQuantity_Gold_Ocr",
                  cost_node="Agt_BuyQuantity_Cost_Ocr")
    shop_expected = ("^" + re.escape(_clean(request["shop_name"])) + "$"
                     if request.get("shop_name") else _cart_expected(request["cartridge"]))
    patch.update({
        "Arbitrage_Sell_HUB": {"anchor": {"Sell_Bypass": ""}},
        "Arbitrage_Sell_Type_Ocr": {"any_of": ["Arbitrage_Buy_Button_Chg"]},
        "Arbitrage_Sell_Type_Clr": {"roi": [118, 94, 36, 59]},
        "Arbitrage_Sell_PackShopSwich": {"expected": shop_expected},
        "Arbitrage_Sell_PackShopSwich_PostOcr": {
            "recognition": "DirectHit", "post_wait_freezes": 0,
            "next": ["Arbitrage_Sell_Item_ListTraverse", "Arbitrage_PreciseBuy_NotFound"]},
        "Rec_<Arbitrage_Sell_Item_SellMenu>_Ocr_01": {"expected": [_BUY_BUTTON]},
        "Arbitrage_Sell_Item_Price_MaxCheck": {"roi": [810, 450, 222, 102], "expected": _BUY_BUTTON},
        _CONFIRM: {"expected": [_BUY_BUTTON], "action": "Custom",
                   "custom_action": "ArbitrageBuyConfirm", "custom_action_param": request,
                   "post_delay": 0, "timeout": 4000, "next": [_EXIT], "on_error": [_EXIT]},
        "Arbitrage_Sell_Gold_Snapshot": {"action": "DoNothing"},
        "Arbitrage_Sell_Item_Click": {"target_offset": [0, 30, 0, 0]},
        "Arbitrage_Sell_End": {"action": "DoNothing", "focus": "Arb.补买：本批返回"},
        _QUANTITY: {"custom_action": "ArbitrageBuyQuantity", "custom_action_param": request,
                    "attach": config, "next": [_EXIT] if request.get("dry_run")
                    else ["Arbitrage_Sell_Item_Selling"]},
    })
    for node in ("Arbitrage_ItemList_Swip", "Arbitrage_Sell_Item_ListTraverse_End",
                 "Arbitrage_ItemList_Sorting_Entry", "Arbitrage_Sell_TypeB"):
        patch[node] = {"enabled": False}
    return patch


def run_batch(context, request):
    # 调用局部上下文：购买词、色核、旁支和数量动作不会改写后续出售流程。
    local = context.clone()
    batch = {**request, "request_id": uuid4().hex}
    for node in ("Arbitrage_Sell_Item_Cancel", "Arbitrage_Sell_Item_Exit_ResetSwip",
                 "Arbitrage_Sell_Item_ListTraverse"):
        if not local.clear_hit_count(node):
            return {"status": "skipped", "return_ok": False, "reason": f"无法清除本批计数: {node}"}, False
    error = None
    detail = None
    try:
        detail = local.run_task("Arbitrage_Sell_HUB", buy_overrides(local, batch))
    except Exception as exc:
        error = str(exc)
    finally:
        result = take_buy_result(batch["request_id"])
    attempted = bool(result and result.get("purchase_attempted"))
    if detail is None:
        return {**(result or {"status": "unknown"}), "return_ok": False,
                "reason": error or "补买流程未能启动"}, attempted
    names = {node.name for node in detail.nodes}
    if "Arbitrage_Sell_Item_Exit_Failed" in names:
        return {**(result or {"status": "unknown"}), "return_ok": False,
                "reason": "补买子页未能复位"}, attempted
    if not detail.status.succeeded or "Arbitrage_Sell_End" not in names:
        return {**(result or {"status": "unknown"}), "return_ok": False,
                "reason": "补买流程未确认正常退出"}, attempted
    if "Arbitrage_PreciseBuy_NotFound" in names:
        return {"status": "not_found"}, False
    if result is None and not attempted and "Arbitrage_Sell_Item_Click" in names:
        if "Arbitrage_Sell_Item_SellMenu" not in names:
            # 未点击购买且页面已恢复，不能打开只表示跳过，不声称售罄。
            image = local.tasker.controller.post_screencap().wait().get()
            if image is None or not image.size:
                return {"status": "skipped", "return_ok": False, "reason": "补买退出截图失败"}, False
            visible = local.run_recognition("Arbitrage_Replenish_ShopReady", image)
            if visible is None or not visible.hit:
                return {"status": "skipped", "return_ok": False,
                        "reason": "补买未打开子页，且未确认返回商店列表"}, False
            mfaalog.warning(f"[PreciseBuy] [{request['item_name']}] 名称存在但购买子页未打开，本批跳过")
            return {"status": "skipped", "reason": "item_menu_unavailable"}, False
    return result or {"status": "unknown"}, attempted


def verify_purchase(context, request, ready):
    """两帧稳定金币与本批报价精确相符才记账，不重开可能已售罄的商品。"""
    local = context.clone()
    config = dict(context.get_node_object(_QUANTITY).attach)
    config["gold_node"] = "Agt_BuyQuantity_Gold_Ocr"
    if not local.override_pipeline({_CONFIRM: {"recognition": "Custom", "custom_recognition": "GoldVerdict",
            "custom_recognition_param": {"node": config["gold_node"], "direction": "decrease",
                                         "expected_delta": -ready["quoted_total"]}}}):
        raise RuntimeError("购买金币核对配置失败")
    adjuster = BuyQuantityAdjuster(local, config, request)
    previous = None
    reason = "购后金币读数未稳定"
    for attempt in range(adjuster.read_attempts):
        if attempt:
            time.sleep(adjuster.read_interval)
        image = adjuster.capture()
        try:
            after = parse_money(adjuster.texts("gold_node", image))
            verdict = local.run_recognition(_CONFIRM, image)
            matched = bool(verdict is not None and verdict.hit)
            value = (after, matched)
            if value == previous:
                spent = ready["gold"] - after
                if matched and spent == ready["quoted_total"] and get_baseline() == ready["gold"]:
                    amount = ready["selected"]
                    return {"status": "confirmed" if amount == request["target"] else "partial",
                            "actual_quantity": amount, "actual_spent": spent, "gold_after": after,
                            "owned_after": ready["owned"] + amount,
                            "remaining_stock": ready["available"] - amount,
                            "inventory_source": "gold_confirmed", "remaining_stock_source": "calculated"}
                reason = f"金币差额未确认：减少{spent}，本批报价{ready['quoted_total']}"
            previous = value
        except ValueError as exc:
            previous = None
            reason = f"购后金币读不清：{exc}"
    return {"status": "unknown", "actual_quantity": None, "actual_spent": None, "reason": reason}


def execute_buy(context, request):
    if not isinstance(request, dict):
        raise ValueError("补买请求必须为对象")
    if not isinstance(request.get("item_name"), str) or not request["item_name"].strip():
        raise ValueError("缺少item_name")
    if request.get("shop_name"):
        if not isinstance(request["shop_name"], str) or not request["shop_name"].strip():
            raise ValueError("柜台完整名称无效")
        if request.get("cartridge"):
            raise ValueError("柜台名与卡带编号不可同时指定")
    elif not isinstance(request.get("cartridge"), str) or not re.search(r"\d+$", request["cartridge"].strip()):
        raise ValueError("补买缺少完整柜台名或有效卡带尾号")
    for field in ("target", "max_unit_price", "budget"):
        _integer(request.get(field), field)
    dry_run = request.get("dry_run", True)
    if type(dry_run) is not bool:
        raise ValueError("dry_run必须为布尔值")
    request = {**request, "dry_run": dry_run}
    clear_verdict()
    try:
        ready, attempted = run_batch(context, request)
        ready["purchase_attempted"] = attempted
        if attempted and not dry_run and ready["status"] == "ready":
            try:
                return {**ready, **verify_purchase(context, request, ready)}
            except Exception as exc:
                return {**ready, "status": "unknown", "actual_quantity": None, "actual_spent": None,
                        "reason": str(exc)}
        if attempted or ready["status"] == "unknown":
            return {**ready, "status": "unknown", "actual_quantity": None, "actual_spent": None}
        status = "prepared" if dry_run and ready["status"] == "ready" else ready["status"]
        if status == "ready":
            status = "skipped"
            ready.setdefault("reason", "购买确认未点击")
        return {**ready, "status": status, "actual_quantity": 0, "actual_spent": 0}
    finally:
        clear_verdict()


@AgentServer.custom_action("ArbitrageBuyController")
class ArbitrageBuyController(CustomAction):
    def run(self, context, argv):
        request_id = None
        result = None
        try:
            raw = argv.custom_action_param
            request = raw if isinstance(raw, dict) else json.loads(str(raw))
            if isinstance(request, dict):
                request_id = request.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id):
                request_id = None
                raise ValueError("请求编号无效")
            result = execute_buy(context, request)
            mfaalog.info(f"[PreciseBuy] 补买结果: {result}")
            return result["status"] in ("prepared", "confirmed", "partial", "skipped", "not_found")
        except Exception as exc:
            result = {"status": "unknown", "reason": str(exc)}
            mfaalog.warning(f"[PreciseBuy] 补买未确认: {exc}")
            return False
        finally:
            if request_id is not None and result is not None:
                _RESULTS[request_id] = result
