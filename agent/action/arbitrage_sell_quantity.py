"""出售子页单批选量：核对条件、操作数量控件，确认后才放行原有出售节点。"""

import json
import re
import time
import unicodedata

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import mfaalog
from utils.name_i18n import canon
from utils.ocr_item_name import resolve_item_ocr
from utils.arbitrage_quote import parse_quote
from utils.arbitrage_sale_state import get_batch


_COUNT = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"
_RESULTS: dict[str, dict] = {}


def _clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def plan_quantity(inventory, reserve, target=None):
    """计算计划上限；实际单批还须受当前打开堆叠的MAX读数限制。"""
    _integer(inventory, "库存")
    _integer(reserve, "保留量", -1)
    if target is not None:
        _integer(target, "剩余待售量")
    if reserve == -1:
        return 0
    available = max(inventory - reserve, 0)
    return available if target is None else min(available, target)


def parse_quantity(texts, inventory=False):
    """选量可与金额粘连，只取完整的数量单位；不拼接OCR块、不猜残缺数字。"""
    values = set()
    for raw in texts:
        text = _clean(raw)
        if inventory:
            match = re.fullmatch(r"(?:拥有|持有)(" + _COUNT + r")[个個]", text)
            matches = [match] if match else []
        else:
            matches = re.finditer(r"(?<![\w,.+−-])(" + _COUNT + r")[个個]", text)
        values.update(int(match[1].replace(",", "")) for match in matches)
    if len(values) != 1:
        raise ValueError(f"{'库存' if inventory else '待售数量'}没有唯一完整读数: {texts!r}")
    return values.pop()


def take_result(request_id):
    """结果只表示选量阶段，不证明已点击或成交；由派发者按请求编号领取。"""
    return _RESULTS.pop(request_id, None)


class QuantityAdjuster:
    def __init__(self, context, config, request):
        self.context = context
        self.config = config
        self.request = request
        self.name = request.get("item_name")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("未指定出售物品")
        self.name = canon(_clean(self.name))
        if "reserve" not in request:
            raise ValueError("未指定保留量")
        plan_quantity(0, request["reserve"], request.get("target"))
        self.deadline = time.monotonic() + _integer(config["timeout_seconds"], "调数时限", 1)
        self.max_adjustments = _integer(config["max_adjustments"], "调数次数上限", 1)
        self.read_attempts = _integer(config["read_attempts"], "读数次数", 2)
        self.read_interval = _integer(config.get("read_interval_ms", 150), "复读间隔") / 1000
        self.adjustments = 0

    def check_running(self):
        if self.context.tasker.stopping:
            raise RuntimeError("任务已停止")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("数量调整超过时限")

    def capture(self):
        self.check_running()
        image = self.context.tasker.controller.post_screencap().wait().get()
        if image is None or not image.size:
            raise RuntimeError("出售子页截图失败")
        return image

    def recognize(self, key, image):
        result = self.context.run_recognition(self.config[key], image)
        if result is None:
            raise RuntimeError(f"识别未执行: {self.config[key]}")
        return result

    def texts(self, key, image):
        result = self.recognize(key, image)
        if not result.hit:
            raise ValueError(f"未读到{self.config[key]}")
        items = getattr(result, "filtered_results", None) or getattr(result, "all_results", None) or []
        return [item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
                for item in items]

    def inventory_state(self, image):
        if not self.recognize("menu_node", image).hit:
            raise ValueError("出售子页未打开")
        result = self.recognize("name_node", image)
        resolved = [resolve_item_ocr(self.context, self.config["name_node"], image, item)
                    for item in (getattr(result, "filtered_results", None) or [])]
        names = {item["name"] for item in resolved}
        if not resolved or any(not item["confirmed"] for item in resolved) or names != {self.name}:
            raise ValueError(f"买卖名称未确认或不符: 期望{self.name}，实际{resolved}")
        return parse_quantity(self.texts("inventory_node", image), inventory=True)

    def state(self, image):
        inventory = self.inventory_state(image)
        if not self.recognize("price_node", image).hit:
            raise ValueError("出售价格未通过本轮行情核对")
        return inventory

    def read(self, full=False):
        """两次连续截图读数一致才接受，短暂空读/跳数不能成为最后的出售依据。"""
        previous = None
        reason = "出售数量未稳定"
        for attempt in range(self.read_attempts):
            if attempt:
                time.sleep(self.read_interval)
            image = self.capture()
            try:
                if full:
                    value = (self.state(image), parse_quantity(self.texts("selected_node", image)))
                else:
                    if not self.recognize("menu_node", image).hit:
                        raise ValueError("调整期间出售子页消失")
                    value = parse_quantity(self.texts("selected_node", image))
                if value == previous:
                    return value
                previous = value
            except ValueError as exc:
                reason = str(exc)
                previous = None
        raise RuntimeError(reason)

    def action(self, key, x=None):
        self.check_running()
        if self.adjustments >= self.max_adjustments:
            raise RuntimeError("数量调整达到次数上限")
        node = self.config[key]
        override = {}
        if x is not None:
            target = list(self.context.get_node_object(node).action.param.target)
            target[0] = x
            override = {node: {"target": target}}
        result = self.context.run_action(node, pipeline_override=override)
        self.adjustments += 1
        if result is None or not result.success:
            raise RuntimeError(f"数量控件操作失败: {node}")

    def adjust(self, target, maximum, *, max_selected=None):
        if max_selected is None:
            self.action("max_node")
            current = self.read()
        else:
            current = max_selected
        if current != maximum:
            raise RuntimeError(f"MAX选量{current}与本批控件上限{maximum}不符")
        if current == target:
            return current

        # 靠近两端时直接微调；大跨度先按滑块像素二分，不依赖全范围线性。
        if target - 1 <= 100 and target - 1 < maximum - target:
            self.action("min_node")
            current = self.read()
            if current != 1:
                raise RuntimeError(f"MIN未回到1: {current}")
        elif maximum - target > 100:
            bounds = self.config["slider_range"]
            if (not isinstance(bounds, list) or len(bounds) != 2
                    or any(type(value) is not int for value in bounds)
                    or not 0 <= bounds[0] < bounds[1] < 1280):
                raise ValueError("滑块范围配置无效")
            low, high = bounds
            best = None
            while low <= high:
                x = (low + high) // 2
                self.action("slider_node", x)
                current = self.read()
                if not 1 <= current <= maximum:
                    raise RuntimeError(f"滑块选量超出范围: {current}")
                if best is None or abs(current - target) < abs(best[1] - target):
                    best = (x, current)
                if current == target:
                    return current
                if current < target:
                    low = x + 1
                else:
                    high = x - 1
            if best is not None and current != best[1]:
                self.action("slider_node", best[0])
                current = self.read()

        while current != target:
            before = current
            diff = target - current
            key = ("plus_" if diff > 0 else "minus_") + ("ten_node" if abs(diff) >= 10 else "one_node")
            self.action(key)
            current = self.read()
            if not 1 <= current <= maximum or abs(current - target) >= abs(before - target):
                raise RuntimeError(f"数量调整未接近目标: {before}→{current}，目标{target}")
        return current

    def prepare(self):
        self.check_running()
        if self.request["reserve"] == -1 or self.request.get("target") == 0:
            return {"status": "skipped", "reason": "无限保留或本次目标为0"}
        inventory, initial_selected = self.read(full=True)
        target = plan_quantity(inventory, self.request["reserve"], self.request.get("target"))
        if target == 0:
            return {"status": "skipped", "inventory": inventory, "target": 0, "reason": "已达到保留量"}
        # 拥有量是所有堆叠的总量，MAX只覆盖当前打开的一叠，不假定固定堆叠上限。
        # 先确认起点为1，避免MAX失效时把上次残留的选量当成当前上限。
        if initial_selected != 1:
            self.action("min_node")
            minimum = self.read()
            if minimum != 1:
                raise RuntimeError(f"MIN未回到1: {minimum}")
        self.action("max_node")
        maximum = self.read()
        if not 1 <= maximum <= inventory:
            raise RuntimeError(f"MAX选量{maximum}不在库存允许范围1～{inventory}内")
        if maximum == 1 and inventory > 1:
            raise RuntimeError("MAX后仍选中1个，无法确认当前堆叠上限")
        target = min(target, maximum)
        selected = self.adjust(target, maximum, max_selected=maximum)
        final_inventory, final_selected = self.read(full=True)
        if final_inventory != inventory or final_selected != target or selected != target:
            raise RuntimeError("最终库存或待售数量改变，取消本批")
        return {"status": "ready", "inventory": inventory, "selected": final_selected,
                "target": target, "reserve": self.request["reserve"], "maximum": maximum,
                "adjustments": self.adjustments}


class SaleQuantityAdjuster(QuantityAdjuster):
    """出售专用的最终整行报价及回读；购买继续使用基础数量控件。"""

    def observe_sale(self, inventory_only=False):
        previous = None
        reason = "出售库存或整行报价不稳定"
        for attempt in range(self.read_attempts):
            if attempt:
                time.sleep(self.read_interval)
            image = self.capture()
            try:
                value = self.inventory_state(image) if inventory_only else (
                    self.state(image), *parse_quote(self.texts("quote_node", image)))
                if value == previous:
                    return value
                previous = value
            except ValueError as exc:
                reason, previous = str(exc), None
        raise RuntimeError(reason)

    def prepare(self):
        batch = get_batch(self.request.get("request_id")) if self.request.get("managed_sale") else None
        if batch is not None and batch.status == "rechecking":
            # 先结算上一笔，再检查是否要卖；价格改变、目标已为0也不能挡掉回读。
            remaining = batch.reconcile(self.observe_sale(inventory_only=True))
            if remaining == 0:
                return {"status": "confirmed", "inventory": batch.inventory_after,
                        "reason": "库存证明上一笔已经全部成交，不再补卖"}
            self.request["target"] = remaining
        result = super().prepare()
        if result["status"] != "ready":
            if batch is not None:
                batch.status = "skipped"
                batch.inventory_after = result.get("inventory", batch.inventory_after)
                batch.reason = result["reason"]
            return result
        inventory, selected, total = self.observe_sale()
        if inventory != result["inventory"] or selected != result["selected"] or total <= 0:
            raise RuntimeError("整行报价的库存、选量或总额与本批不一致")
        if batch is not None and batch.status == "new" and "expected_inventory" in self.request:
            if inventory != self.request["expected_inventory"]:
                batch.uncertain = True
                raise RuntimeError("续批库存与上一批确认结余不符")
        result["quoted_total"] = total
        return result


@AgentServer.custom_action("ArbitrageSellQuantity")
class ArbitrageSellQuantity(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        request_id = None
        result = None
        batch = None
        try:
            raw = argv.custom_action_param
            request = raw if isinstance(raw, dict) else json.loads(str(raw))
            if not isinstance(request, dict):
                raise ValueError("数量请求必须为对象")
            request_id = request.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id):
                request_id = None
                raise ValueError("请求编号无效")
            if request.get("managed_sale"):
                from .arbitrage_sell_batch import check_scope
                batch = get_batch(request_id)
                check_scope(context, batch)
            config = context.get_node_object(argv.node_name).attach
            result = SaleQuantityAdjuster(context, config, dict(request)).prepare()
            if result["status"] == "ready":
                if batch is not None:
                    from . import gold_verify
                    if gold_verify.get_baseline() is None:
                        raise RuntimeError("本批售前金币不可读，不放行出售")
                    if not context.override_pipeline({"Arbitrage_Sell_Item_AmountConfirmed": {
                        "custom_recognition_param": {"direction": "increase",
                                                     "expected_delta": result["quoted_total"]},
                    }}):
                        raise RuntimeError("本批金额确认参数注入失败")
                    check_scope(context, batch)
                    batch.arm(result)
                mfaalog.info(f"[SellQuantity] [{request['item_name']}] 库存{result['inventory']}，"
                             f"保留{result['reserve']}，当前堆叠MAX{result['maximum']}，"
                             f"本批选量{result['selected']}，"
                             f"实际报价{result['quoted_total']}已核对")
                return True
            mfaalog.info(f"[SellQuantity] 跳过本批: {result['reason']}")
            return False
        except Exception as exc:
            result = {"status": "rejected", "reason": str(exc)}
            if batch is not None:
                batch.fail(exc)
            mfaalog.warning(f"[SellQuantity] 取消本批: {exc}")
            return False
        finally:
            if request_id is not None and result is not None:
                _RESULTS[request_id] = result
