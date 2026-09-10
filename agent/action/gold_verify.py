"""金币测量：链内取两次读数，判定交给主控（2026-08-06）。

## 为什么拆成链内测量 + 链外判定

原先主控在 `run_task` 前后各读一次金币自行判成败，问题是那对读数跨越了整条出售链
（进出售菜单 → 复位卡带表 → 选柜台 → 找物品 → 卖），时间窗长、容易被无关的金币变动
污染；而链条内部到底走到哪一步、卖了几件，主控一概不知。

改为链内埋两个动作节点：

- **A `GoldSnapshot`** —— 挂在「物品已被 OCR 确认存在」之后、点击之前，记基准值。
  每次执行都清除旧基准和结论，再覆盖记录新基准；读数失败也不会留下旧基准。
  重试回边应放在基准节点之后，不能在成交后重新记录购前值。
- **B `GoldVerdict`** —— 挂在 `Arbitrage_Sell_End`（复位态确认）上，取终值算差额。

两者只**测量**，不判"卖成没卖成" —— 判据归主控，它才知道还有没有别的候选要试。
主控与本模块在同一个 agent 进程（`AgentServer` 注册的 custom 都在 `agent/main.py`
那一个进程里），所以结论走模块级槽位直接交接，不必再测第二次。

多开时每个实例有独立的 agent 进程，模块级槽位天然隔离；**不要把它挪成跨进程的
文件/环境变量等系统级共享**。

## 时序契约

    主控 clear_verdict()  →  run_task(Sell_HUB)  →  [链内] A 写基准
                                                 →  [链内] B 消费基准、写结论
                          →  主控 take_verdict() 取走

B 未执行（链条中途断了、没走到复位态）时 `take_verdict()` 返回 None，主控按
「无法判断」处理 —— 当作没卖成功，有候选就换候选，但不拿同样的参数重跑一遍。

## Pipeline 直接接入

- action GoldSnapshot：custom_action_param.node 可选，默认 Arbitrage_Sell_GoldRead。
  每次覆盖基准并清旧结论；读取失败仍返回 True，但后续金币识别不会命中。
- recognition GoldVerdict：custom_recognition_param.node 同上；direction 必填，
  decrease 表示金币减少（购买），increase 表示金币增加（出售）。
  使用本次识别画面；无基准、读不到或方向不符均不命中。不消耗基准、不写结论。
- action GoldClear：无参数，清除基准和结论；重复清除无害。
- 原 action GoldVerdict 保留给出售主控：消费基准、保存差额，恒返回 True。
  与 recognition GoldVerdict 名称相同，但由不同字段选择，勿混用。

下面是购买弹窗已打开后的接线示例，复用项目现有确认按钮识别和购买金币 ROI。
Example_Gold_Snapshot 只进一次；金币未变化时继续等待，超时清除后结束，不打标。
金币减少只能证明发生支出，不能单独证明全部收藏已买空或页面已经恢复。
清除放在成功节点的 action 和放弃出口，避免下一笔误用旧基准；不要放在识别器内部。
示例的成功 next 接现有打标节点；页面恢复沿用该节点后续流程。

{
    "Example_Gold_Snapshot": {
        "action": "Custom",
        "custom_action": "GoldSnapshot",
        "custom_action_param": {"node": "Agt_BuyQuantity_Gold_Ocr"},
        "next": ["Example_Gold_Confirm"]
    },
    "Example_Gold_Confirm": {
        "recognition": "Or",
        "any_of": ["Arbitrage_Favorit_Buy_SubMenu_BuyAction"],
        "action": "Click",
        "post_delay": 5000,
        "timeout": 10000,
        "next": ["Example_Gold_Purchased"],
        "on_error": ["Example_Gold_Abort"]
    },
    "Example_Gold_Purchased": {
        "recognition": "Custom",
        "custom_recognition": "GoldVerdict",
        "custom_recognition_param": {
            "node": "Agt_BuyQuantity_Gold_Ocr",
            "direction": "decrease"
        },
        "action": "Custom",
        "custom_action": "GoldClear",
        "next": ["Arbitrage_Buy_Select_End"]
    },
    "Example_Gold_Abort": {
        "action": "Custom",
        "custom_action": "GoldClear",
        "focus": "购买成交未确认，本轮结束"
    }
}

出售侧识别用法：将 direction 改为 increase，并换成出售金币节点和自己的 next。
使用 action GoldVerdict 的旧主控仍用 clear_verdict() / take_verdict() 交接，
不要在主控领取结论前执行 GoldClear；识别器不会为 take_verdict() 生成结论。
"""

import json
import re
from typing import Optional

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils import mfaalog

# 默认出售金币读数节点；购买侧通过 node 参数指定自己的 ROI。
_GOLD_NODE_DEFAULT = "Arbitrage_Sell_GoldRead"
# 位数上界：超过这个位数不可能是真金币，是把别的元素也吃进来了。
_GOLD_MAX_DIGITS = 12

# Snapshot 覆盖；动作 Verdict 消费，识别 Verdict 只读，Clear 清除。
_BASELINE: Optional[dict] = None
# B 写、主控一次性消费。
_VERDICT: Optional[dict] = None


def _node_of(argv) -> str:
    """custom_action_param 可传 {"node": "..."} 换金币读数节点（购买侧界面不同时用）。

    param 可能是 dict 也可能是 JSON 字符串，两种都接。
    """
    raw = getattr(argv, "custom_action_param", None)
    if not raw:
        return _GOLD_NODE_DEFAULT
    try:
        parsed = raw if isinstance(raw, dict) else json.loads(str(raw).strip())
        node = parsed.get("node") if isinstance(parsed, dict) else None
        if isinstance(node, str) and node.strip():
            return node.strip()
    except (ValueError, TypeError) as e:
        mfaalog.warning(f"[Gold] ⚠️ custom_action_param 解析失败({e})，沿用默认节点")
    return _GOLD_NODE_DEFAULT


def _read_gold(context: Context, node: str = _GOLD_NODE_DEFAULT, *, image=None) -> Optional[int]:
    """读金币数。读不到 / 读数不合理 → None。

    返回 None 而不是 0：0 得留给「玩家真的没钱」。旧写法用 0 兼做「读不到」的哨兵，
    调用侧 `if gold_before and gold_after` 把两者混为一谈。

    候选筛选（旧写法是「位数 >= 4 且取数值最大」，两条都有坑）：

    · **位数下限取 1**。旧的 >= 4 会让金币不足 1000 的号验证永久失效 —— "900" 被整条
      滤掉，返回 0，主控判「金币不可读」；卖出后涨到 1200 也一样，因为基准值读不到。
    · **前导零即误读，丢弃**。金币显示不带前导零，"0900" 这种一定是把图标之类多认了
      一位。单个 "0" 保留（真没钱）。同 `_rescue_tail_num` 对尾号「1~99 无前导 0」的
      合理性判据。
    · **多个候选存活时取 det 框最宽的**，不是取数值最大的。金币串一定是这个窄 roi 里
      最长的文本；而「取最大值」在低金币时会翻车 —— 真金币 900、噪声碎片读成 12345，
      取最大就取到碎片了。
    """
    try:
        ss = image if image is not None else context.tasker.controller.post_screencap().wait().get()
        if ss is None:
            mfaalog.warning("[Gold] ⚠️ 截图失败，本次读数作废")
            return None
        reco = context.run_recognition(node, ss)
        if not reco:
            # 识别没跑起来（节点名错/被禁用/图像空）——与「跑了但没认出数字」不是一回事，
            # 分开报，免得照着日志去查 OCR 而其实是节点问题。
            mfaalog.warning(f"[Gold] ⚠️ 识别节点 [{node}] 未能执行，本次读数作废")
            return None

        best_val, best_w = None, -1
        dropped = []
        for m in getattr(reco, "all_results", None) or []:
            text = getattr(m, "text", "") or ""
            digits = re.sub(r"\D", "", text)
            if not digits:
                continue
            if len(digits) > 1 and digits[0] == "0":
                dropped.append(f"{text!r}(前导零)")
                continue
            if len(digits) > _GOLD_MAX_DIGITS:
                dropped.append(f"{text!r}({len(digits)}位超上界)")
                continue
            # box 可能是 Rect(有 .w) 也可能是 list[x,y,w,h]，两种都接。
            box = getattr(m, "box", None)
            w = getattr(box, "w", None)
            if w is None:
                w = box[2] if isinstance(box, (list, tuple)) and len(box) > 2 else 0
            if w > best_w:
                best_val, best_w = int(digits), w

        if best_val is None:
            mfaalog.warning(
                f"[Gold] ⚠️ [{node}] 未读到合理金币数"
                + (f"（丢弃候选：{'、'.join(dropped)}）" if dropped else "（roi 内无数字）")
            )
        elif dropped:
            mfaalog.debug(f"[Gold] 读数 {best_val:,}，丢弃候选：{'、'.join(dropped)}")
        return best_val
    except Exception as e:
        mfaalog.warning(f"[Gold] ⚠️ 金币读取异常({e})，本次读数作废")
        return None


# ==========================================
# 主控侧接口
# ==========================================
def clear_verdict() -> None:
    """清除基准和结论；纯 Pipeline 可调用 GoldClear 动作。"""
    global _BASELINE, _VERDICT
    _BASELINE = None
    _VERDICT = None


def take_verdict() -> Optional[dict]:
    """收包后取走本轮结论并清空。

    返回 `{"before": int|None, "after": int|None, "delta": int|None}`；
    **None 表示 B 压根没执行**（链条中途断了，没走到复位态）。
    `delta is None` 表示 B 执行了但两端读数至少缺一个 —— 两种都是「无法判断」。
    """
    global _VERDICT
    verdict, _VERDICT = _VERDICT, None
    return verdict


# ==========================================
# 链内测量点
# ==========================================
@AgentServer.custom_action("GoldSnapshot")
class GoldSnapshot(CustomAction):
    """A：记基准金币。挂在「物品已确认存在」之后、点击出售之前。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _BASELINE
        clear_verdict()
        gold = _read_gold(context, _node_of(argv))
        if gold is not None:
            _BASELINE = {"gold": gold}
        mfaalog.info(
            f"[Gold] 📌 基准 {gold:,}" if gold is not None else "[Gold] 📌 基准读数不可用"
        )
        # 恒 True：金币是次要保险，读不到不该把主流程拦在这儿（物品存在性才是主判据）。
        # 返回 False 会走 on_error，还平白多一张错误截图。
        return True


@AgentServer.custom_action("GoldClear")
class GoldClear(CustomAction):
    """成功或放弃本轮测量时清除基准和结论。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        clear_verdict()
        return True


@AgentServer.custom_recognition("GoldVerdict")
class GoldVerdictRecognition(CustomRecognition):
    """按金币增减识别；只读本轮基准，不消费或写入测量状态。"""

    def analyze(self, context, argv):
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            if not isinstance(params, dict):
                raise ValueError("参数必须为对象")
            direction = params.get("direction")
            if direction not in ("decrease", "increase"):
                raise ValueError("direction 必须为 decrease 或 increase")
            node = params.get("node", _GOLD_NODE_DEFAULT)
            if not isinstance(node, str) or not node.strip():
                raise ValueError("node 必须为非空识别节点名")
            baseline = _BASELINE
            if baseline is None or baseline.get("gold") is None:
                return None
            if argv.image is None:
                return None
            before = baseline["gold"]
            after = _read_gold(context, node.strip(), image=argv.image)
            if after is None:
                return None
            delta = after - before
            if (direction == "decrease" and delta < 0) or (direction == "increase" and delta > 0):
                return CustomRecognition.AnalyzeResult(
                    box=(0, 0, 1, 1),
                    detail={"before": before, "after": after, "delta": delta, "direction": direction},
                )
            return None
        except Exception as exc:
            mfaalog.warning(f"[Gold] 金币差额识别失败：{exc}")
            return None


@AgentServer.custom_action("GoldVerdict")
class GoldVerdict(CustomAction):
    """B：取终值算差额，落槽位交主控。挂在 `Arbitrage_Sell_End`（复位态确认）上。

    只算不判 —— 出售通常按 delta > 0、购买按 delta < 0，具体判据由主控定。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _BASELINE, _VERDICT
        # 一次性消费：基准取走即清。A之后断链时仍须由放弃出口或下一轮入口清理。
        baseline, _BASELINE = _BASELINE, None
        before = baseline.get("gold") if baseline else None
        after = _read_gold(context, _node_of(argv))
        delta = (after - before) if (before is not None and after is not None) else None

        # 同一趟 run_task 里 B 可能被触发不止一次(链条结构使然,实测「基准/差额」会成对出现
        # 两次)。第二次起基准已被首次一次性消费掉,delta 必然是 None —— 绝不能让这个空结论
        # 覆盖掉已经算出来的差额,否则主控会把一件明明卖成了的物品报成「无法判断」,还要多跑
        # 一个候选柜台。只降不升的覆盖一律挡掉。
        if delta is None and _VERDICT is not None and _VERDICT.get("delta") is not None:
            mfaalog.debug(f"[Gold] ↺ 重复触发，保留已成立的差额 {_VERDICT['delta']:+,}")
            return True

        _VERDICT = {"before": before, "after": after, "delta": delta}

        if delta is None:
            miss = "未取到基准（A 没跑）" if before is None else "终值读数不可用"
            mfaalog.warning(f"[Gold] ➖ 差额无法计算：{miss}")
        else:
            mfaalog.info(f"[Gold] 💰 差额 {delta:+,}（{before:,} → {after:,}）")
        return True
