# -*- coding: utf-8 -*-
"""SmartAction —— 代理执行任意动作 + 帧差分决定去向。"""

import json
import time

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

import utils

__version__ = "1.1.0"

# ==============================================================================
# SmartAction  v1.1.0   （SmartSwipe 的泛化，设计契约见 priv/doc/agent/SmartAction/）
# ==============================================================================
# [它是什么]
# 代理执行一个 Pipeline 节点（动作随便），前后各截一帧比对 detect_roi，
# 按「画面变没变」决定这个 custom 节点返回成功还是失败、或者在 py 内再来一轮。
#
# [为什么需要它]
# 「画面变没变」是 Pipeline 唯一表达不了的判断 —— 框架有 post_wait_freezes 能"等"
# 画面静止，但没有跨动作前后帧比对的识别器，无法"据此分支"。**py 侧只做这一比特，
# 别的判据（等某物出现/消失之类）一概走 Pipeline，不要加进来。**
#
# [三层结构]
#   宿主节点(action: Custom) → 本 py → proxy_node（原生节点，由框架执行）
# 代理执行而非直接调 controller，是为了继承框架的拟人化曲线、end_hold 惯性保持，
# 也让动作参数留在 Pipeline 里可被 interface / pipeline_manager 覆盖。
#
# ------------------------------------------------------------------------------
# 决策表：态当 key，行为当 value
# ------------------------------------------------------------------------------
# 帧差分只产出一比特（变 / 没变），但要驱动三个语义：继续循环 / 正常完成 / 真的出错。
# bool 返回值只有 next / on_error 两个通道，装不下三个 —— 所以让 py **消化掉"循环"
# 这一态**，剩下两态正好对两个通道，且映射可显式配置。
#
#   on_changed    画面变了怎么办    loop | next | error
#   on_unchanged  画面没变怎么办    loop | next | error
#
#   loop  = 不返回，py 内继续下一轮
#   next  = return True   → 走宿主节点的 next
#   error = return False  → 走宿主节点的 on_error
#
# 没有第四种"退栈"：退栈由宿主节点上 next / on_error **写不写**决定，不归 py 选。
#
# ------------------------------------------------------------------------------
# unchanged_streak：为什么复检只挂在"没变"这一侧
# ------------------------------------------------------------------------------
# 阴性结论与阳性证据不对称 ——
#   "没变" 是**没观察到**变化，一次采样不足以下结论，必须连续多次才可信（验尸）；
#   "变了" 是**观察到了**证据，一次即确凿，再试反而多做了几次动作，有害。
# 所以复检恒定只对"没变"计数，与哪个态在 loop 无关。
#
#   unchanged_streak = **总次数**（不是"额外次数"）。1 = 一次没变就下结论 = 不复检。
#   ⚠️ 旧 SmartSwipe 的 retry_times 是额外次数（总数 = 1 + retry_times），迁移要 +1。
#
# 复检时**会重复执行 proxy**（与旧版一致）—— 反复尝试才叫验尸。
#
# 于是两个场景的计数逻辑是同一个，区别只在数满之后走哪个通道：
#   滑到底：数"连续几次没滑动"，满了 → 到底了
#   点到生效：数"连续几次点了没反应"，满了 → 点不动，放弃
#
# ⚠️ 但复检只对**结论**有意义。loop 不是结论 —— 它是"不下结论，再试一次"。
#    在它前面挂复检，等于"多看几眼，然后决定不下结论"，下一轮还问同样的问题。
# ⇒ on_unchanged 配成 loop 时 unchanged_streak **不生效**（写了会 warning），
#    重试次数改由 loop_limit 管。两个参数各管一维，互不重叠：
#
#      unchanged_streak             下结论前的复检门槛
#                                   → 仅 on_unchanged 是结论(next/error)时生效
#      loop_limit / loop_exhausted  loop 的配套安全阀
#                                   → 哪一侧配了 loop 就管哪一侧
#
#    所以 loop 在两侧语义完全对称：都计 loops、都受 loop_limit 约束、都从
#    loop_exhausted 出。决策表六个格子全合法，没有例外要记。
#
#    ⚠️ 唯一的禁配是**两侧同时 loop** —— 那就成了纯靠 loop_limit 兜的死循环，
#    日志里看不出是配错了，所以在 _parse 里直接拒绝。
#
# ------------------------------------------------------------------------------
# JSON 参数
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "SmartAction",
# "custom_action_param": {
#     // ── 做什么（py 不解释内容，整包透传给 run_task）
#     "proxy_node": "Agt_Proxy_Blank",   // [必填] 代理节点名
#     "proxy_override": {                // [选填] 不写 = 用 proxy 节点自带的参数(预设模式)
#         "action": "Swipe",             //        写了 = 字段级覆盖(内联/混合模式)
#         "begin": [285, 660],
#         "end": [285, 135],
#         "duration": 600,
#         "end_hold": 1000,
#         "post_delay": 1000
#     },
#
#     // ── 怎么看
#     "detect_roi": [195, 104, 52, 120], // [必填] 比对区域 [x,y,w,h]，w/h 是宽高不是右下角
#     "threshold": 3.0,                  // [选填] 平均绝对差(MAD) 0~255，越小越严格
#     "settle_delay": 500,               // [选填] 动作后再截图前的等待 ms（UI 回弹）
#
#     // ── 决策表
#     "on_changed": "next",              // [选填] 默认 next
#     "on_unchanged": "error",           // [选填] 默认 error
#     "unchanged_streak": 3,             // [选填] 默认 1；on_unchanged:loop 时不生效
#     "loop_limit": 20,                  // [选填] 默认 20；哪一侧配了 loop 就管哪一侧
#     "loop_exhausted": "error"          // [选填] 默认 error；只接受 next/error
# }
#
# threshold 方向与 TemplateMatch(0~1) **相反**：这里越小越相似。
#   0.0      两帧二进制一致
#   1.0~3.0  视觉静止（容渲染噪点、光影微变）—— 推荐默认 3.0
#   > 5.0    画面确实位移了
#
# ------------------------------------------------------------------------------
# 复现旧 SmartSwipe（零行为变更迁移）
# ------------------------------------------------------------------------------
#   "on_changed": "next"                  变了立刻返回成功，循环留在 Pipeline 层自环
#   "on_unchanged": "error"               到底了返回失败 → on_error → 退栈
#   "unchanged_streak": 1 + retry_times
#   proxy_override 里补 "post_delay": 1000
#       ↑ 旧 Custom_Proxy_Swipe 节点自带 post_delay:1000 且旧 override 没覆盖它，
#         所以那 1000ms 一直是生效的。换成真空 proxy 节点后会掉到协议默认 200ms，
#         **不补上就不是零行为变更**。
# ==============================================================================

_ACTS = ("loop", "next", "error")


@AgentServer.custom_action("SmartAction")
class SmartAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        tag = f"[SmartAction:{getattr(argv, 'node_name', '?')}]"
        try:
            cfg = self._parse(argv, tag)
            if cfg is None:
                return False
            return self._loop(context, cfg, tag)
        except Exception as e:
            utils.mfaalog.error(f"{tag} ❌ 异常: {e}")
            return False

    # ------------------------------------------------------------------
    # 参数解析与校验：一律明确报错，不静默降级
    # ------------------------------------------------------------------
    # CustomAction 没有 detail 通道（RunResult 只有 success 一个字段，C 回调也没有
    # out 参数），失败原因进不了框架的节点详情 —— 所以这里的日志是唯一的诊断入口，
    # 格式必须固定、可 grep。
    def _parse(self, argv: CustomAction.RunArg, tag: str):
        raw = argv.custom_action_param
        if not raw:
            utils.mfaalog.error(f"{tag} ❌ 缺少 custom_action_param")
            return None
        if isinstance(raw, dict):
            params = raw
        else:
            try:
                params = json.loads(str(raw).strip())
            except ValueError as e:
                utils.mfaalog.error(f"{tag} ❌ 参数 JSON 解析失败: {e}")
                return None
        # json.loads 成功不代表拿到了 object —— 参数写成 "abc" / [1,2] 时下面的 .get
        # 会抛 AttributeError，落进 run 的笼统异常分支，配置错误就查不出是哪一条。
        if not isinstance(params, dict):
            utils.mfaalog.error(f"{tag} ❌ custom_action_param 必须是 object，实际: {type(params).__name__}")
            return None

        proxy_node = params.get("proxy_node")
        if not proxy_node:
            utils.mfaalog.error(f"{tag} ❌ 缺少 proxy_node")
            return None

        detect_roi = params.get("detect_roi")
        if not isinstance(detect_roi, (list, tuple)) or len(detect_roi) != 4:
            utils.mfaalog.error(f"{tag} ❌ detect_roi 必须是 [x,y,w,h]，实际: {detect_roi!r}")
            return None

        on_changed = str(params.get("on_changed", "next")).strip().lower()
        on_unchanged = str(params.get("on_unchanged", "error")).strip().lower()
        loop_exhausted = str(params.get("loop_exhausted", "error")).strip().lower()

        for key, val in (("on_changed", on_changed), ("on_unchanged", on_unchanged)):
            if val not in _ACTS:
                utils.mfaalog.error(f"{tag} ❌ {key} 只接受 loop/next/error，实际: {val!r}")
                return None
        if on_changed == "loop" and on_unchanged == "loop":
            utils.mfaalog.error(f"{tag} ❌ on_changed 与 on_unchanged 不能同时为 loop（这是死循环）")
            return None
        # loop_exhausted 是 loop 的出口，再填 loop 就没有出口了。想无限转请调大 loop_limit。
        if loop_exhausted not in ("next", "error"):
            utils.mfaalog.error(f"{tag} ❌ loop_exhausted 只接受 next/error，实际: {loop_exhausted!r}")
            return None

        try:
            threshold = float(params.get("threshold", 3.0))
            settle_delay = int(params.get("settle_delay", 500))
            unchanged_streak = int(params.get("unchanged_streak", 1))
            # 默认 20 而不是更大的数：它同时也是 on_unchanged:"loop" 的重试上限，而那一侧
            # 每一轮都是原地重试（不像 on_changed:"loop" 每轮都在推进），转太久纯属白等。
            # 定位与 timeout 一样 —— 有个够用的默认值，要精确控制就自己写。
            loop_limit = int(params.get("loop_limit", 20))
        except (TypeError, ValueError) as e:
            utils.mfaalog.error(f"{tag} ❌ 数值参数解析失败: {e}")
            return None

        if threshold <= 0 or settle_delay < 0 or unchanged_streak < 1 or loop_limit < 1:
            utils.mfaalog.error(
                f"{tag} ❌ 数值越界: threshold={threshold} settle_delay={settle_delay} "
                f"unchanged_streak={unchanged_streak} loop_limit={loop_limit}"
            )
            return None

        # loop 不是结论，复检在它前面是空转 —— 这一侧的次数归 loop_limit 管。不拒绝配置，
        # 但必须说出来：静默吃掉一个写了的参数，等于让人对着日志猜为什么跑了 N 轮。
        if on_unchanged == "loop" and "unchanged_streak" in params:
            utils.mfaalog.warning(
                f"{tag} ⚠️ on_unchanged 为 loop 时 unchanged_streak={unchanged_streak} 不生效，"
                f"重试次数由 loop_limit={loop_limit} 决定"
            )

        override = params.get("proxy_override")
        if override is not None and not isinstance(override, dict):
            utils.mfaalog.error(f"{tag} ❌ proxy_override 必须是 object，实际: {type(override).__name__}")
            return None

        return {
            "proxy_node": proxy_node,
            "proxy_override": {proxy_node: override} if override else None,
            "detect_roi": list(detect_roi),
            "threshold": threshold,
            "settle_delay": settle_delay / 1000.0,
            "on_changed": on_changed,
            "on_unchanged": on_unchanged,
            "unchanged_streak": unchanged_streak,
            "loop_limit": loop_limit,
            "loop_exhausted": loop_exhausted,
        }

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _loop(self, context: Context, cfg: dict, tag: str) -> bool:
        streak = 0     # 连续"没变"的次数
        loops = 0      # 显式 loop 的次数（隐式的复检轮不计入，它自带 streak 上界）
        rounds = 0

        while True:
            rounds += 1
            before = self._grab(context, cfg["detect_roi"], tag)
            if before is None:
                return False

            if not self._fire(context, cfg, tag, rounds):
                return False

            if cfg["settle_delay"] > 0:
                time.sleep(cfg["settle_delay"])

            after = self._grab(context, cfg["detect_roi"], tag)
            if after is None:
                return False

            diff = self._diff(before, after, tag)
            if diff is None:
                return False

            if diff >= cfg["threshold"]:
                streak = 0
                act = cfg["on_changed"]
                utils.mfaalog.info(f"{tag} ✅ 第{rounds}轮 画面已改变 (diff={diff:.2f} ≥ {cfg['threshold']}) → {act}")
            elif cfg["on_unchanged"] == "loop":
                # 这一侧不数 streak：loop 不是结论，复检挂在它前面是空转（见文件头）。
                # 次数归 loop_limit 管，与 on_changed:"loop" 完全对称。
                act = "loop"
                utils.mfaalog.warning(
                    f"{tag} ⚠️ 第{rounds}轮 画面未改变 (diff={diff:.2f}) "
                    f"— 重试 {loops + 1}/{cfg['loop_limit']}"
                )
            else:
                streak += 1
                if streak < cfg["unchanged_streak"]:
                    utils.mfaalog.warning(
                        f"{tag} ⚠️ 第{rounds}轮 画面未改变 (diff={diff:.2f}) "
                        f"— 复检中 {streak}/{cfg['unchanged_streak']}"
                    )
                    continue
                act = cfg["on_unchanged"]
                utils.mfaalog.info(
                    f"{tag} 🛑 第{rounds}轮 画面未改变 (diff={diff:.2f}) "
                    f"— 已连续 {streak} 次确认 → {act}"
                )

            if act != "loop":
                return act == "next"

            loops += 1
            if loops >= cfg["loop_limit"]:
                utils.mfaalog.warning(
                    f"{tag} 🛑 loop 已达上限 {cfg['loop_limit']} → {cfg['loop_exhausted']}"
                )
                return cfg["loop_exhausted"] == "next"

    # ------------------------------------------------------------------
    # 代理执行
    # ------------------------------------------------------------------
    def _fire(self, context: Context, cfg: dict, tag: str, rounds: int) -> bool:
        """执行 proxy。返回 False 表示"压根没跑起来"，应立即中止。

        proxy 可以是一条节点链（override 里的 next / on_error 不剥），整体当一个 try 块看待：
        链内部怎么失败的不做区分，因为动作可能已部分生效，让帧差分说话。
        """
        node = cfg["proxy_node"]
        detail = context.run_task(node, cfg["proxy_override"])
        # 旧版把返回值整个丢弃 → 代理节点写错/被禁用时动作压根没发生，而前后两帧必然
        # 一致 → diff=0 → 被判成"画面没变" → 配置错误就这样被静默翻译成了业务结论。
        if detail is None:
            utils.mfaalog.error(
                f"{tag} ❌ 第{rounds}轮 代理节点 [{node}] 未能启动"
                f"（节点不存在/被禁用/正在停止），动作未执行"
            )
            return False
        if not detail.status.succeeded:
            utils.mfaalog.warning(f"{tag} ⚠️ 第{rounds}轮 代理节点 [{node}] 非正常结束，动作可能未生效")
        return True

    # ------------------------------------------------------------------
    # 帧差分
    # ------------------------------------------------------------------
    def _grab(self, context: Context, roi, tag: str):
        """截图并裁剪 detect_roi。失败返回 None —— 不得降级成"画面没变"。"""
        img = context.tasker.controller.post_screencap().wait().get()
        if img is None or img.size == 0:
            utils.mfaalog.error(f"{tag} ❌ 截图获取失败")
            return None
        area = self._parse_area(roi, img.shape)
        if area is None:
            utils.mfaalog.error(f"{tag} ❌ detect_roi {roi} 在 {img.shape[1]}x{img.shape[0]} 画面上取不到有效区域")
            return None
        x, y, w, h = area
        return img[y : y + h, x : x + w]

    def _diff(self, img1, img2, tag: str):
        """平均绝对差(MAD)。无法比对时返回 None —— 旧版这里返回 0.0("数学上撒谎，
        业务上安全")，那个"安全"只在"没变 = 提前跳出"时成立；本类的 on_unchanged
        可以配成 next，同一个谎就变成"假装成功"了，所以必须让它可区分地失败。"""
        if img1 is None or img2 is None:
            utils.mfaalog.error(f"{tag} ❌ 比对失败：有一帧为空")
            return None
        if img1.shape != img2.shape:
            utils.mfaalog.error(f"{tag} ❌ 比对失败：两帧尺寸不一致 {img1.shape} vs {img2.shape}")
            return None
        # 空切片过得了上面的尺寸检查（两帧同为 (h,0,3)），而 np.mean 对空数组返回 nan，
        # nan >= threshold 恒为 False —— 又变回"静默判成画面没变"。_parse_area 已从源头
        # 堵死，这里是最后一道，不靠上游正确性。
        if img1.size == 0:
            utils.mfaalog.error(f"{tag} ❌ 比对失败：ROI 区域为空 {img1.shape}")
            return None
        # 转 float 防 uint8 减法溢出（2 - 5 会变成 253）。不转灰度，直接对 BGR 三通道
        # 求均值 —— 效果与加权灰度一致，甚至更灵敏。
        return float(np.mean(np.abs(img1.astype(float) - img2.astype(float))))

    def _parse_area(self, area, img_shape):
        """按框架 ROI 规则把 [x,y,w,h] 转成 numpy 可用的绝对坐标。画面退化时返回 None。

        ⚠️ x/y 的上界是 **边长-1** 而不是边长：钳到边长本身时 w/h 的 max(1,...) 仍会切出
        空数组（img[y:y+1, w_img:w_img+1]），而空数组一路畅通到 _diff 变成 nan，最终被
        判成"画面没变"。上界收一格，切片必含至少一个像素。
        """
        x, y, w, h = area
        h_img, w_img = img_shape[:2]
        if w_img < 1 or h_img < 1:
            return None

        # 负 x/y：从右/下边缘起算
        if x < 0:
            x += w_img
        if y < 0:
            y += h_img
        # 负 w/h：取绝对值，(x,y) 视为右下角
        if w < 0:
            w, x = abs(w), x - abs(w)
        if h < 0:
            h, y = abs(h), y - abs(h)
        # 0 w/h：延伸到边缘
        if w == 0:
            w = w_img - x
        if h == 0:
            h = h_img - y

        x = max(0, min(int(x), w_img - 1))
        y = max(0, min(int(y), h_img - 1))
        w = max(1, min(int(w), w_img - x))
        h = max(1, min(int(h), h_img - y))
        return x, y, w, h
