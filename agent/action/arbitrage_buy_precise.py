"""按显式请求复用出售HUB补买；共用定位与调数，购买条件和结果单独核对。"""

import json
import re
import time
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .arbitrage_sell_quantity import QuantityAdjuster, parse_quantity, _clean, _integer, _COUNT
from utils import mfaalog
from utils.name_i18n import canon


_QUANTITY = "Arbitrage_Sell_Item_Quantity"
_EXIT = "Arbitrage_Sell_Item_Exit"
_RESULTS = {}
_MONEY = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:[,.][0-9]{3})+)"


def take_buy_result(request_id):
    """按调用者提供的request_id领取结果；选量/报价不等于实际买入/支出。"""
    return _RESULTS.pop(request_id, None)


def parse_money(texts, cost=False):
    values = set()
    for raw in texts:
        text = _clean(raw)
        if cost:
            text = re.sub(r"^(?:" + _COUNT + r")?[个個]", "", text)
        if re.fullmatch(_MONEY, text):
            values.add(int(text.replace(",", "").replace(".", "")))
    if len(values) != 1:
        raise ValueError(f"金额没有唯一完整读数: {texts!r}")
    return values.pop()


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
        cost = parse_money(self.texts("cost_node", image), cost=True)
        return owned, available, gold, cost

    def observe(self):
        """购后只复核实际收据字段；选量、价格或按钮变灰不影响库存差额读取。"""
        previous = None
        reason = "购买后库存、店余或金币未稳定"
        for attempt in range(self.read_attempts):
            if attempt:
                time.sleep(self.read_interval)
            image = self.capture()
            try:
                if not self.recognize("menu_node", image).hit:
                    raise ValueError("购买子页未打开")
                names = {canon(_clean(text)) for text in self.texts("name_node", image) if text}
                if names != {self.name}:
                    raise ValueError(f"购买名称不符: 期望{self.name}，实际{names}")
                value = (parse_quantity(self.texts("inventory_node", image), inventory=True),
                         parse_available(self.texts("available_node", image)),
                         parse_money(self.texts("gold_node", image)))
                if value == previous:
                    return {"status": "observed", "owned": value[0], "available": value[1], "gold": value[2]}
                previous = value
            except ValueError as exc:
                reason = str(exc)
                previous = None
        raise RuntimeError(reason)

    def prepare(self):
        self.check_running()
        if self.request.get("verify_only"):
            return self.observe()
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


def buy_overrides(context, request):
    from .arbitrage_result import _sell_item_override, _cart_expected
    patch = _sell_item_override(context, request["item_name"])
    config = dict(context.get_node_object(_QUANTITY).attach)
    config.update(available_node="Agt_BuyQuantity_Available_Ocr", gold_node="Agt_BuyQuantity_Gold_Ocr",
                  cost_node="Agt_BuyQuantity_Cost_Ocr")
    shop_expected = ("^" + re.escape(_clean(request["shop_name"])) + "$"
                     if request.get("shop_name") else _cart_expected(request["cartridge"]))
    patch.update({
        "Arbitrage_Sell_HUB": {"anchor": {"Sell_Bypass": ""}},
        "Arbitrage_Sell_Type_Ocr": {"expected": ["购买"], "roi": [66, 80, 140, 78]},
        "Arbitrage_Sell_Type_Clr": {"roi": [118, 94, 36, 59]},
        "Arbitrage_Sell_PackShopSwich": {"expected": shop_expected},
        "Arbitrage_Sell_PackShopSwich_Clr": {"next": [
            "Arbitrage_Sell_Item_ListTraverse", "Arbitrage_PreciseBuy_NotFound"]},
        "Rec_<Arbitrage_Sell_Item_SellMenu>_Ocr_01": {"expected": ["购买"]},
        "Arbitrage_Sell_Item_Price_MaxCheck": {"roi": [810, 450, 222, 102], "expected": "购买"},
        "Arbitrage_Sell_Item_Selling": {"expected": ["购买"]},
        "Arbitrage_Sell_Gold_Snapshot": {"action": "DoNothing"},
        "Arbitrage_Sell_End": {"action": "DoNothing", "focus": "Arb.补买：本批返回"},
        _QUANTITY: {"custom_action": "ArbitrageBuyQuantity", "custom_action_param": request,
                    "attach": config, "next": [_EXIT] if request.get("dry_run") or request.get("verify_only")
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
    for node in ("Arbitrage_Sell_Item_Cancel", "Arbitrage_Sell_Item_Exit_ResetSwip"):
        if not local.clear_hit_count(node):
            raise RuntimeError(f"无法清除本批计数: {node}")
    try:
        detail = local.run_task("Arbitrage_Sell_HUB", buy_overrides(local, batch))
    finally:
        result = take_buy_result(batch["request_id"])
    if detail is None:
        raise RuntimeError("补买流程未能启动")
    names = {node.name for node in detail.nodes}
    if "Arbitrage_Sell_Item_Exit_Failed" in names:
        raise RuntimeError("补买子页未能复位")
    if not detail.status.succeeded or "Arbitrage_Sell_End" not in names:
        raise RuntimeError("补买流程未确认正常退出")
    if "Arbitrage_PreciseBuy_NotFound" in names:
        return {"status": "not_found"}, False
    attempted = any(node.name == "Arbitrage_Sell_Item_Selling" and node.action is not None for node in detail.nodes)
    return result or {"status": "unknown"}, attempted


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
    request = {**request, "dry_run": dry_run, "verify_only": False}
    ready, attempted = run_batch(context, request)
    if dry_run:
        if attempted:
            raise RuntimeError("只调数模式意外进入购买确认节点，结果未知")
        return {**ready, "status": "prepared" if ready["status"] == "ready" else ready["status"],
                "actual_quantity": 0, "actual_spent": 0}
    if ready["status"] != "ready" or not attempted:
        unknown = attempted or ready["status"] in ("ready", "unknown")
        return {**ready, "status": "unknown" if unknown else ready["status"],
                "actual_quantity": None if unknown else 0, "actual_spent": None if unknown else 0}
    # 再次打开同一物品只读库存；无论结果如何，都不再次提交购买。
    after, _ = run_batch(context, {**request, "verify_only": True, "dry_run": True})
    result = {**ready, "status": "unknown", "actual_quantity": None, "actual_spent": None}
    if after["status"] == "observed":
        amount = after["owned"] - ready["owned"]
        spent = ready["gold"] - after["gold"]
        if (0 < amount <= ready["selected"] and ready["available"] - after["available"] == amount
                and spent == amount * ready["unit_price"]):
            result.update(status="confirmed" if amount == request["target"] else "partial",
                          actual_quantity=amount, actual_spent=spent, remaining_stock=after["available"],
                          owned_after=after["owned"], gold_after=after["gold"])
    return result


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
