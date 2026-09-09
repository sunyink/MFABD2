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


_COUNT = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"
_RESULTS: dict[str, dict] = {}


def _clean(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def plan_quantity(inventory, reserve, target=None):
    """target=None表示本次没有额外计划上限，实际仍受库存、保留量和99999限制。"""
    _integer(inventory, "库存")
    _integer(reserve, "保留量", -1)
    if target is not None:
        _integer(target, "剩余待售量")
    if reserve == -1:
        return 0
    return min(max(inventory - reserve, 0), 99999, target if target is not None else 99999)


def parse_quantity(texts, inventory=False):
    """选量可与金额粘连，只取完整的数量单位；不拼接OCR块、不猜残缺数字。"""
    values = set()
    for raw in texts:
        text = _clean(raw)
        if inventory:
            match = re.fullmatch(r"[拥擁]有(" + _COUNT + r")[个個]", text)
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

    def state(self, image):
        if not self.recognize("menu_node", image).hit:
            raise ValueError("出售子页未打开")
        names = {canon(_clean(text)) for text in self.texts("name_node", image) if text}
        if names != {self.name}:
            raise ValueError(f"出售名称不符: 期望{self.name}，实际{names}")
        if not self.recognize("price_node", image).hit:
            raise ValueError("出售价格未通过本轮行情核对")
        return parse_quantity(self.texts("inventory_node", image), inventory=True)

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

    def adjust(self, target, maximum):
        self.action("max_node")
        current = self.read()
        if current != maximum:
            raise RuntimeError(f"MAX选量{current}与库存推算上限{maximum}不符")
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
        inventory, _ = self.read(full=True)
        target = plan_quantity(inventory, self.request["reserve"], self.request.get("target"))
        if target == 0:
            return {"status": "skipped", "inventory": inventory, "target": 0, "reason": "已达到保留量"}
        selected = self.adjust(target, min(inventory, 99999))
        final_inventory, final_selected = self.read(full=True)
        if final_inventory != inventory or final_selected != target or selected != target:
            raise RuntimeError("最终库存或待售数量改变，取消本批")
        return {"status": "ready", "inventory": inventory, "selected": final_selected,
                "target": target, "reserve": self.request["reserve"], "adjustments": self.adjustments}


@AgentServer.custom_action("ArbitrageSellQuantity")
class ArbitrageSellQuantity(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        request_id = None
        result = None
        try:
            raw = argv.custom_action_param
            request = raw if isinstance(raw, dict) else json.loads(str(raw))
            if not isinstance(request, dict):
                raise ValueError("数量请求必须为对象")
            request_id = request.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not request_id):
                request_id = None
                raise ValueError("请求编号无效")
            config = context.get_node_object(argv.node_name).attach
            result = QuantityAdjuster(context, config, request).prepare()
            if result["status"] == "ready":
                mfaalog.info(f"[SellQuantity] [{request['item_name']}] 库存{result['inventory']}，"
                             f"保留{result['reserve']}，本批选量{result['selected']}已核对")
                return True
            mfaalog.info(f"[SellQuantity] 跳过本批: {result['reason']}")
            return False
        except Exception as exc:
            result = {"status": "rejected", "reason": str(exc)}
            mfaalog.warning(f"[SellQuantity] 取消本批: {exc}")
            return False
        finally:
            if request_id is not None and result is not None:
                _RESULTS[request_id] = result
