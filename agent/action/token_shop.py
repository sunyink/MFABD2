"""代币商店兑换里 pipeline 做不了的两处算术：主页面商店定购买数量（两档共用）、月末窗口。

别的判断（认商品、买得起买不起、翻页、停下）都在 pipeline 里，见 TokenShop.json。
"""

import calendar
import json
import re
import time
from datetime import datetime, timezone

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from .arbitrage_buy_precise import parse_available
from .arbitrage_sell_quantity import QuantityAdjuster
from recognition.counter import TAG_STORE
from utils import mfaalog
from utils.arbitrage_quote import MONEY, clean


# 读数与调数用哪些节点，写在这个数据节点的 attach 里（PC 覆盖包改它的 slider_range）。
_CONFIG = "Agt_TokenShop_Quantity"


def spare_target(balance, reserve, unit, maximum):
    """余量档买几个：买完后余额不低于保留量，且不超过弹窗 MAX 给的数。"""
    if unit < 1 or maximum < 1:
        raise ValueError(f"单价或 MAX 选量无效: {unit}, {maximum}")
    return max(0, min(maximum, (balance - reserve) // unit))


def total_from(text, quantity):
    """从「16個 3,600」这样的一行里取总价，数量已知。OCR 偶尔漏掉「个」字，读成「163.600」（PC 11-22 实测），
    照样按已知数量拆开；中间的代币图标若被读成一个符号也放过。拆不出、或总价不能被数量整除，返回 None。"""
    digits = re.sub(r"[,.，．]", "", clean(text))
    match = re.fullmatch(str(quantity) + r"[个個]?[^0-9]?([0-9]+)\+?", digits)
    if not match:
        return None
    total = int(match[1])
    return total if total and total % quantity == 0 else None


def days_left_in_month(now):
    """本月含今天在内还剩几天。按游戏日算：09:00 KST 换日，正好是 UTC 日期。"""
    day = datetime.fromtimestamp(now, timezone.utc).date()
    return calendar.monthrange(day.year, day.month)[1] - day.day + 1


class _Adjuster(QuantityAdjuster):
    """借套利的数量控件操作与复读（购买弹窗是同一个组件）；名称、行情那些套利专有的核对不走。"""

    def __init__(self, context, config):
        super().__init__(context, config, {"item_name": "代币商店商品", "reserve": 0})

    def total(self, quantity):
        """白字整行报价里的总价。数量已另读（选量那一处），这里只认「数量 + 个 + 总价」，「个」字被漏读也认；
        连续两次一致才算。买不起时总价是红字，被白字过滤掉，读不到，返回 None。"""
        previous = None
        for attempt in range(self.read_attempts):
            if attempt:
                time.sleep(self.read_interval)
            image = self.capture()
            result = self.recognize("cost_node", image)
            items = (getattr(result, "filtered_results", None) or []) if result.hit else []
            texts = [item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "") for item in items]
            value = total_from(texts[0], quantity) if len(texts) == 1 else None
            if value is not None and value == previous:
                return value
            previous = value
        return None

    def balance(self, image):
        """右上角余额。数字左边的代币图标偶尔被读成一个数字，所以只取最右边那个完整的数。"""
        result = self.recognize("balance_node", image)
        items = (getattr(result, "filtered_results", None) or []) if result.hit else []
        found = []
        for item in items:
            text = clean(item.get("text", "") if isinstance(item, dict) else getattr(item, "text", ""))
            box = item.get("box") if isinstance(item, dict) else getattr(item, "box", None)
            if box is not None and re.fullmatch(MONEY, text):
                found.append((box[0] + box[2], int(text.replace(",", "").replace(".", ""))))
        if not found:
            raise ValueError(f"未读到余额: {self.config['balance_node']}")
        return max(found)[1]

    def settle_quantity(self, reserve):
        self.check_running()
        image = self.capture()
        available = parse_available(self.texts("available_node", image))
        # 保留量为 0（必买档）时 MAX 给的数就是要买的数：不用读余额与总价，少两处会读错的地方。
        # 一个都买不起时 MAX 停在 1 个、「购买」是灰的，由 pipeline 看按钮处理。
        balance = self.balance(image) if reserve else None
        self.action("max_node")
        maximum = self.read()
        if not 1 <= maximum <= available:
            raise RuntimeError(f"MAX 后的选量{maximum}不在 1～可购买{available} 之内")
        if balance is None:
            return {"status": "ready", "selected": maximum, "available": available}
        total = self.total(maximum)
        if total is None:
            return {"status": "poor", "balance": balance, "available": available,
                    "reason": "总价不是白字或读不到，按买不起 1 个处理"}
        if total % maximum or total > balance:
            raise RuntimeError(f"MAX 后的读数对不上: 选量{maximum}、总价{total}、可购买{available}、余额{balance}")
        unit = total // maximum
        target = spare_target(balance, reserve, unit, maximum)
        if not target:
            return {"status": "reserved", "balance": balance, "unit": unit, "available": available,
                    "reason": "买 1 个就会低于保留量"}
        if target < maximum:
            self.adjust(target, maximum, max_selected=maximum)
            if self.read() != target or self.total(target) != target * unit:
                raise RuntimeError(f"调到 {target} 个后的数量或总价核对失败")
        return {"status": "ready", "selected": target, "unit": unit, "total": target * unit,
                "balance": balance, "available": available}


@AgentServer.custom_action("TokenShopQuantity")
class TokenShopQuantity(CustomAction):
    """主页面商店定购买数量。购买弹窗已开；保留量取本节点 attach.reserve（必买档 0，余量档是玩家填的数）。

    点 MAX 后游戏给的是「限购剩余」与「余额够买几个」里小的那个；一个都买不起时停在 1 个、总价变红（用户 11-22 实测）。
    返回 True：数量已调好，后面由 pipeline 点「购买」。只买得了一部分（余额或保留量卡住，没买到限购剩余）时
    顺手打开停下标记——这家店买完这一件就不再往后试，等下次。
    返回 False：一个都不能买，或读数核对不上；由节点的 on_error 关弹窗并让这家店停下（读错时宁可少买，用户 11-22 同意）。
    """

    def run(self, context, argv):
        try:
            reserve = (context.get_node_object(argv.node_name).attach or {}).get("reserve")
            if isinstance(reserve, str) and reserve.strip().isdigit():
                reserve = int(reserve)
            if type(reserve) is not int or reserve < 0:
                raise ValueError(f"保留量应为非负整数: {reserve!r}")
            config = dict(context.get_node_object(_CONFIG).attach or {})
            result = _Adjuster(context, config).settle_quantity(reserve)
            mfaalog.info(f"[TokenShop] {argv.node_name} 保留量{reserve}: {result}")
            if result["status"] != "ready":
                return False
            if result["selected"] < result["available"]:
                TAG_STORE[config["halt_tag"]] = -1
            return True
        except Exception as exc:
            mfaalog.warning(f"[TokenShop] {argv.node_name} 定数量失败，这家店停下: {exc}")
            return False


@AgentServer.custom_recognition("TokenShopMonthEnd")
class TokenShopMonthEnd(CustomRecognition):
    """月末窗口：本月含今天在内还剩不超过 days 天才命中。参数 {"days": 5}。"""

    def analyze(self, context, argv):
        try:
            raw = argv.custom_recognition_param
            days = (raw if isinstance(raw, dict) else json.loads(str(raw)))["days"]
            if type(days) is not int or days < 1:
                raise ValueError(f"days 应为正整数: {days!r}")
            left = days_left_in_month(time.time())
            if left > days:
                return None
            return CustomRecognition.AnalyzeResult(box=(0, 0, 0, 0), detail={"days_left": left, "window": days})
        except Exception as exc:
            mfaalog.warning(f"[TokenShop] 月末窗口判断失败，按没到处理: {exc}")
            return None
