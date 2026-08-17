"""Fishing custom action for MaaFramework.

This ports the standalone Python+ADB fishing bot into MFABD2 as a Maa custom
action. It relies on Maa controller APIs for screencap and input, not raw ADB.

Defaults assume the game runs at 1920x1080. The original coordinates were
measured at 1280x720; all points are scaled at runtime based on the current
screenshot resolution. Override timing/strategy via pipeline argv.raw_json if
needed.

Migration Notes:
- cv2 dependency removed; uses MaaFramework pipeline ColorMatch directly for all color detection
- Progress bar analysis uses ColorMatch for cursor and zone tracking
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


# ==================== 参数来源 ====================
# 下列参数全部改由 pipeline JSON 的节点提供,py 只保留一份「回落默认」。
# 【改值一律落 JSON、不动 py】—— py 里的默认值仅在「节点缺失 / 键缺失 / 值非法」时兜底,
# 正常运行时不生效。对应节点见 assets/resource/base/pipeline/Fishing.json:
#   Fishing_Minigame_Data      机制常量(游戏决定,三端共用)
#   Fishing_Minigame_Timing    时序补偿(设备决定,per端可覆盖)
#   Fishing_Minigame_Strategy  策略阈值
#   Fishing_Minigame_Settle    结算点击点
# 另有两处坐标不另设参数,直接取自既有节点 —— 它们本就必须与识别 ROI 严格一致,各存一份
# 必然漂移(本次改造前 progress_bar 的 484/858 与 ROI 的 480/863 已经对不上了):
#   进度条左右边界 <- Rec_FishMinigame_Cursor_Clr.roi
#   拉杆点         <- Casting_Rod.target

_NODE_DATA = "Fishing_Minigame_Data"
_NODE_TIMING = "Fishing_Minigame_Timing"
_NODE_STRATEGY = "Fishing_Minigame_Strategy"
_NODE_SETTLE = "Fishing_Minigame_Settle"
_NODE_CURSOR = "Rec_FishMinigame_Cursor_Clr"
_NODE_CAST = "Casting_Rod"
# 下面两个节点【只存在于 playcover 资源包】。agent 以「节点是否存在」判断当前是不是 iOS 端:
# 存在则启用蓄力抛竿与卖鱼前置,不存在(安卓 / PC)则整段逻辑不参与,base 行为一字不变。
# 判据用的是节点存在性而非控制器类型 —— 控制器类型在 Agent 侧拿不到,而资源包与控制器
# 在 interface.json 里本就是绑定的(PlayCover 资源仅对 PlayCover 控制器可选)。
_NODE_PC_CAST = "Fishing_PlayCover_Cast"
_NODE_PC_SELL = "Fishing_PlayCover_Sell"

# 连续没能蓄力到绿的竿数。HoldCastGreenAction 每次调用都会新建 FishingBot,
# 跨竿的状态只能放模块级。鱼包满是唯一已知会让「环亮但永不变绿」持续发生的原因。
_no_green_streak = 0
# 累计卖鱼次数。放模块级而非实例级:包满自救走的是 HoldCastGreenAction 里新建的
# FishingBot,实例计数会从头算,日志上表现为"第 1 次"反复出现。
_total_sell_count = 0

# 机制常量:游标速度(px/帧)、蓝区单边收缩(px/帧)、游标单程帧数、上述速率的基准帧率、单局时长上界(秒)
_DATA_DEFAULT = {
    "cursor_speed": 4.2,
    "blue_shrink": 0.83,
    "cursor_half_cycle": 88,
    "ref_fps": 60.0,
    "minigame_seconds": 17,
}
# 时序补偿:点击提前量、输入链路延迟、游标复位等待、抛竿后等待、结算等待(全部为秒)
_TIMING_DEFAULT = {
    "click_lead": 0.27,
    "input_comp": 0.045,
    "cursor_reset_wait": 0.6,
    "after_cast": 0.2,
    "after_catch": 3.0,
}
# 策略阈值:攒几条卖一次、单次等待上限(秒)、蓝区预测宽度下限(px)
_STRATEGY_DEFAULT = {
    "sell_interval": 30,
    "wait_cap": 5.0,
    "blue_min_width": 5,
    # 开局等进度条渲染出来的上限(秒),与「判定小游戏已结束」所需的连续无效帧数。
    # 两者都是为了不把「还没画出来」和「偶发漏检」当成「已经钓完」,见 play_minigame。
    "bar_wait_cap": 4.0,
    "end_invalid_frames": 3,
}
# iOS 蓄力抛竿:抛竿按钮外圈蓄力环的圆心/内外半径(px)、判绿的 HSV 阈值与最小绿像素数、
# 蓄力时长上界(秒)、按压未被游戏接收时的重试次数。坐标同为项目统一的 1280×720 基准。
_PC_CAST_DEFAULT = {
    "ring_center_x": 1138,
    "ring_center_y": 570,
    "ring_r_inner": 95,
    "ring_r_outer": 125,
    "green_h_min": 35,
    "green_h_max": 85,
    "green_s_min": 110,
    "green_v_min": 140,
    "green_px_min": 800,
    "max_hold": 10.0,
    "retry_on_no_press": 2,
    # 连续几竿「蓄力环亮着却始终不变绿」就判定鱼包已满并去卖鱼。鱼包满时游戏不让蓄力,
    # 环照亮但颜色停在非绿档 —— 只看「环亮不亮」判断不出来,必须用这个连败计数。
    "no_green_rescue_streak": 3,
}
# iOS 卖鱼前置:退回初始态的中央点击次数、补点次数与间隔(秒)、等待入口出现的上限与轮询间隔(秒)
_PC_SELL_DEFAULT = {
    "center_tap_times": 3,
    "settle_wait": 1.5,
}


def _load_node_attach(context: Context, node: str, defaults: dict) -> dict:
    """读节点 attach,生成**本轮**配置副本(缺项/坏值各自回落 py 默认)。

    绝不原地改写 defaults —— 那样有两个坑(与 arbitrage_result._load_rescue_cfg 同源):
      · 半覆盖:逐项转型中途抛异常被兜住,前几项已写进全局,日志却报「沿用默认」;
      · 粘滞:PatchPipeline 能改 attach,任务结束时框架撤销自己那半边 override,但全局
        dict 它不知道,上一轮的值会一直留着,下一轮 attach 里删了该键也回不到 py 默认。
    """
    cfg = dict(defaults)
    try:
        obj = context.get_node_object(node)
        attach = getattr(obj, "attach", None) if obj else None
        if not attach:
            print(f"warn: 节点 [{node}] 无 attach 配置，本轮沿用 py 内置默认")
            return cfg
        for k, dv in defaults.items():
            if k not in attach:
                continue
            try:
                cfg[k] = type(dv)(attach[k])
            except (TypeError, ValueError):
                print(
                    f"warn: [{node}] 的 {k}={attach[k]!r} 非法"
                    f"（应为 {type(dv).__name__}），该项回落默认 {dv!r}"
                )
    except Exception as e:
        print(f"warn: 读取 [{node}] 配置异常（{e}），整份沿用 py 内置默认")
    return cfg


def _load_optional_attach(context: Context, node: str, defaults: dict) -> Optional[dict]:
    """读「可选节点」的 attach:节点不存在时安静地返回 None,不打 warn。

    与 _load_node_attach 的区别只在缺节点时的语义 —— 那边缺节点是异常(该端本该有这份
    配置),这边缺节点是正常(当前不是该端)。共用一个函数会让安卓端每轮刷两行无意义的
    warn,所以单开一个。节点存在但键缺失/值非法时,仍按 defaults 逐项回落。
    """
    try:
        obj = context.get_node_object(node)
    except Exception:
        return None
    if obj is None:
        return None
    attach = getattr(obj, "attach", None)
    cfg = dict(defaults)
    if not attach:
        return cfg
    for k, dv in defaults.items():
        if k not in attach:
            continue
        try:
            cfg[k] = type(dv)(attach[k])
        except (TypeError, ValueError):
            print(f"warn: [{node}] 的 {k}={attach[k]!r} 非法（应为 {type(dv).__name__}），回落 {dv!r}")
    return cfg


def _node_field(context: Context, node: str, field: str, default=None):
    """读节点的协议字段(roi / target 等)。节点或字段缺失时回落 default。"""
    try:
        data = context.get_node_data(node)
        val = (data or {}).get(field)
        if val:
            return val
        print(f"warn: 节点 [{node}] 未取到 {field}，回落内置默认")
    except Exception as e:
        print(f"warn: 读取 [{node}].{field} 异常（{e}），回落内置默认")
    return default


@dataclass
class TimingCfg:
    after_cast: float = 0.2       # <- Fishing_Minigame_Timing.attach
    after_catch: float = 3.0      # <- 同上
    # 下面两项目前无任何调用方:wait_for_fish 已被 pipeline 的 Casting_Rod->Detect_Took_Bait
    # 取代,input_delay 从未被读过(实际生效的是 Timing.attach 的 input_comp)。保留待
    # iOS / PC 适配时再评估是否复用,故也未纳入 JSON。
    wait_fish_interval: float = 0.08
    input_delay: float = 0.055


@dataclass
class CoordCfg:
    """坐标容器。cast_rod / settle / progress_bar_* 由 _load_coords 在运行时从节点填入,
    这里的字面值只是节点读不到时的兜底。"""

    cast_rod: Tuple[int, int] = (1130, 570)
    settle: Tuple[int, int] = (640, 360)
    progress_bar_left: int = 480
    progress_bar_right: int = 863
    # 无任何调用方,保留待后续评估(同 TimingCfg 末两项)。
    minigame_area: Tuple[int, int, int, int] = (335, 505, 600, 154)


def _load_coords(context: Context) -> CoordCfg:
    """从 pipeline 节点取坐标:进度条边界取自游标识别 ROI,拉杆点取自 Casting_Rod。"""
    c = CoordCfg()
    roi = _node_field(context, _NODE_CURSOR, "roi")
    if roi and len(roi) >= 3:
        c.progress_bar_left = int(roi[0])
        c.progress_bar_right = int(roi[0]) + int(roi[2])
    cast = _node_field(context, _NODE_CAST, "target")
    if cast and len(cast) >= 2:
        c.cast_rod = (int(cast[0]), int(cast[1]))
    settle = _node_field(context, _NODE_SETTLE, "target")
    if settle and len(settle) >= 2:
        c.settle = (int(settle[0]), int(settle[1]))
    return c


class FishingBot:
    def __init__(
        self,
        context: Context,
        sell_interval: Optional[int] = None,
        timing: TimingCfg | None = None,
        coords: CoordCfg | None = None,
    ):
        self.context = context
        self.controller = context.tasker.controller

        # 三份配置在此一次性装载,之后全程只读 self.cfg_* / self.timing / self.coords
        self.cfg_data = _load_node_attach(context, _NODE_DATA, _DATA_DEFAULT)
        self.cfg_timing = _load_node_attach(context, _NODE_TIMING, _TIMING_DEFAULT)
        self.cfg_strategy = _load_node_attach(context, _NODE_STRATEGY, _STRATEGY_DEFAULT)
        # iOS 专属:节点只在 playcover 包里,非该端为 None,相关逻辑整段不参与
        self.cfg_pc_cast = _load_optional_attach(context, _NODE_PC_CAST, _PC_CAST_DEFAULT)
        self.cfg_pc_sell = _load_optional_attach(context, _NODE_PC_SELL, _PC_SELL_DEFAULT)

        # 优先级:custom_action_param > 节点 attach > py 内置默认
        self.sell_interval = (
            sell_interval if sell_interval is not None else self.cfg_strategy["sell_interval"]
        )
        self.timing = timing or TimingCfg(
            after_cast=self.cfg_timing["after_cast"],
            after_catch=self.cfg_timing["after_catch"],
        )
        self.coords = coords or _load_coords(context)

        # runtime stats
        self.running = False
        self.fish_count = 0
        self.success_count = 0
        self.fish_since_last_sell = 0
        self.total_sell_count = 0
        # iOS 蓄力抛竿的上一轮观测:峰值绿像素数,以及蓄力环是否亮过(= 按压被接收)
        self.last_hold_best_green = 0
        self.last_hold_registered = False

    # ============ Controller wrappers ============
    def tap(self, x: float, y: float):
        job = self.controller.post_click(int(x), int(y))
        start_time = time.time()
        job.wait()
        elapsed = time.time() - start_time
        print(f"    🖱️ 点击 ({int(x)}, {int(y)}) 耗时 {elapsed:.3f}s")

    def long_press(self, x: float, y: float, duration_ms: int = 1000):
        # emulate long press via swipe with zero distance
        job = self.controller.post_swipe(int(x), int(y), int(x), int(y), duration_ms)
        job.wait()

    def swipe(self, start_x: float, start_y: float, end_x: float, end_y: float, duration_ms: int = 500):
        job = self.controller.post_swipe(int(start_x), int(start_y), int(end_x), int(end_y), duration_ms)
        job.wait()

    def get_screenshot(self) -> Optional[Any]:
        job = self.controller.post_screencap()
        return job.wait().get()

    def delay(self, seconds: float):
        time.sleep(seconds)

    # ============ Detection methods ============
    def detect_exclamation(self, screenshot: Any) -> bool:
        """Detect fish hook indicator using pipeline TemplateMatch.
        
        Uses Detect_Took_Bait template matching for more accurate detection.
        """
        # Run pipeline recognition (MAA handles resolution scaling automatically)
        # run_recognition 返回 None 表示识别流程压根没起来(节点缺失/被禁用/图像为空),
        # 与"跑了但没命中"是两回事。旧写法直接取 .hit 会 AttributeError,而异常穿过
        # ctypes 回调只在 stderr 留一段无前缀 traceback,GUI 里什么都看不到。
        # (原先此处还每帧 print 整个 RecognitionDetail —— 它含 raw_image/draw_images
        #  等 ndarray,repr 体积很大,而 stdout 是与 UI 的管道,已一并移除。)
        reco_result = self.context.run_recognition("Detect_Took_Bait", screenshot)
        if reco_result is None:
            print("error: ❌ 识别节点 [Detect_Took_Bait] 未能启动（节点缺失/被禁用/图像为空）")
            return False
        return reco_result.hit

    def analyze_progress_bar(self, screenshot: Any):
        """Analyze progress bar using ColorMatch recognitions.
        
        Uses ColorMatch to detect:
        - White cursor position
        - Blue zones
        - Yellow zones
        """
        result = {"cursor_x": None, "blue_regions": [], "yellow_regions": [], "valid": False}
        
        cursor_result = self.context.run_recognition(_NODE_CURSOR, screenshot)
        blue_result = self.context.run_recognition("Rec_FishMinigame_BlueZone_Clr", screenshot)
        yellow_result = self.context.run_recognition("Rec_FishMinigame_YellowZone_Clr", screenshot)

        # 三个识别节点任一"没起来"都属配置错误,而不是"这一帧没看到"。
        # 整帧判无效并只打一条日志 —— 这里是 minigame 的每帧路径,不能逐个刷屏。
        if cursor_result is None or blue_result is None or yellow_result is None:
            missing = [
                n for n, r in (
                    (_NODE_CURSOR, cursor_result),
                    ("Rec_FishMinigame_BlueZone_Clr", blue_result),
                    ("Rec_FishMinigame_YellowZone_Clr", yellow_result),
                ) if r is None
            ]
            print(f"error: ❌ 进度条识别节点未能启动: {', '.join(missing)}")
            return result

        # Detect white cursor
        if cursor_result.hit and cursor_result.best_result is not None:
            # Calculate cursor x from bounding box center of best match
            box = cursor_result.best_result.box
            cursor_x = box[0] + box[2] // 2
            result["cursor_x"] = cursor_x

        # Detect blue zones - get all detected regions
        if blue_result.hit:
            # Extract regions from all matches
            blue_regions = []
            for match in blue_result.all_results:
                box = match.box
                start_x = box[0]
                end_x = box[0] + box[2]
                blue_regions.append((start_x, end_x))
            result["blue_regions"] = blue_regions
        
        # Detect yellow zones - get all detected regions
        if yellow_result.hit:
            # Extract regions from all matches
            yellow_regions = []
            for match in yellow_result.all_results:
                box = match.box
                start_x = box[0]
                end_x = box[0] + box[2]
                yellow_regions.append((start_x, end_x))
            result["yellow_regions"] = yellow_regions
        
        # Validate result
        result["valid"] = result["cursor_x"] is not None and (
            len(result["blue_regions"]) > 0 or len(result["yellow_regions"]) > 0
        )

        print("Progress bar analysis result:", result)
        return result

    def _get_cursor_direction_from_frame(self, frame_count: int) -> int:
        """
        根据帧数计算游标方向
        游标从最左侧到最右侧需要88帧，然后反向
        
        Args:
            frame_count: 当前帧数
            
        Returns:
            int: 1=向右，-1=向左
        """
        # 计算当前在第几个周期内
        half = self.cfg_data["cursor_half_cycle"]
        cycle_frame = frame_count % (half * 2)  # 一个完整周期 = 右行 half + 左行 half
        return 1 if cycle_frame < half else -1

    def _calculate_blue_region_zero_frame(self, blue_regions: List[Tuple[int, int]]) -> Optional[int]:
        """计算蓝色区域多少帧后会收缩归0"""
        if len(blue_regions) == 0:
            return None
        all_starts = [start for start, end in blue_regions]
        all_ends = [end for start, end in blue_regions]
        leftmost = min(all_starts)
        rightmost = max(all_ends)
        blue_center = (leftmost + rightmost) / 2
        distance_to_center = abs(rightmost - blue_center)
        frames_to_zero = distance_to_center / self.cfg_data["blue_shrink"]
        return int(frames_to_zero)

    def _calculate_click_timing(
        self,
        cursor_x: int,
        yellow_regions: List[Tuple[int, int]],
        current_frame: int,
    ) -> Optional[float]:
        """计算游标到达黄色区域的最佳点击时机
        
        Args:
            cursor_x: 当前游标 X 坐标
            yellow_regions: 黄色区域列表 [(start, end), ...]
            current_frame: 当前帧数
        
        Returns:
            float or None: 应该等待的秒数，None 表示无法/不应该点击
        """
        if len(yellow_regions) == 0:
            return None
        
        bar_left = self.coords.progress_bar_left
        bar_right = self.coords.progress_bar_right
        
        # 取黄色区域最靠近游标的一侧作为目标
        yellow_start, yellow_end = yellow_regions[0]
        
        # 根据当前帧数计算游标方向
        cursor_direction = self._get_cursor_direction_from_frame(current_frame)
        
        target_x = yellow_start if cursor_direction > 0 else yellow_end
        
        # 计算距离（考虑方向）
        distance = target_x - cursor_x
        
        # 判断是否需要等待反弹
        # 如果游标向右移动但目标在左边，或游标向左移动但目标在右边
        # 需要计算反弹后的距离
        if cursor_direction > 0 and distance < 0:
            # 游标向右，目标在左边 -> 需要先到右边界反弹
            distance_to_right = bar_right - cursor_x
            distance_back = bar_right - target_x
            total_distance = distance_to_right + distance_back
        elif cursor_direction < 0 and distance > 0:
            # 游标向左，目标在右边 -> 需要先到左边界反弹
            distance_to_left = cursor_x - bar_left
            distance_back = target_x - bar_left
            total_distance = distance_to_left + distance_back
        else:
            # 游标正在向目标移动
            total_distance = abs(distance)
        
        # 计算需要的帧数和时间
        frames_needed = total_distance / self.cfg_data["cursor_speed"]
        time_needed = frames_needed / self.cfg_data["ref_fps"]
        
        # 如果时间太长（超过5秒），可能计算有误或游戏状态变化
        if time_needed > self.cfg_strategy["wait_cap"]:
            return None
        
        return time_needed

    def _calculate_blue_click_timing(
        self,
        cursor_x: int,
        blue_regions: List[Tuple[int, int]],
        current_frame: int,
    ) -> Optional[float]:
        """计算游标到达蓝色区域的最佳点击时机
        考虑蓝色区域会向中心收缩
        
        Args:
            cursor_x: 当前游标 X 坐标
            blue_regions: 蓝色区域列表 [(start, end), ...]
            current_frame: 当前帧数
        
        Returns:
            float or None: 应该等待的秒数，None 表示无法/不应该点击
        """
        if len(blue_regions) == 0:
            return None
        
        # 合并所有蓝色区域，找到最左侧和最右侧
        all_starts = [start for start, end in blue_regions]
        all_ends = [end for start, end in blue_regions]
        blue_start = min(all_starts)
        blue_end = max(all_ends)
        
        # 计算蓝色区域的中心位置
        blue_center = (blue_start + blue_end) / 2
        
        # 根据当前帧数计算游标方向
        cursor_direction = self._get_cursor_direction_from_frame(current_frame)
        
        # 计算游标到达当前蓝色区域中心的距离和时间
        distance = blue_center - cursor_x
        
        # 游标正在向目标移动
        total_distance = abs(distance)
        
        # 计算需要的帧数
        frames_needed = total_distance / self.cfg_data["cursor_speed"]
        
        # 计算在这段时间内，蓝色区域会收缩多少
        # 蓝色区域从两端向中心收缩，每帧收缩 cfg_data["blue_shrink"] 像素
        # 假设蓝色区域的左边界向右移动，右边界向左移动，各收缩一半
        shrink_distance = self.cfg_data["blue_shrink"] * frames_needed
        
        # 预测到达时蓝色区域的新位置
        predicted_blue_start = blue_start + shrink_distance
        predicted_blue_end = blue_end - shrink_distance
        
        # 检查预测的蓝色区域是否还够宽(阈值 blue_min_width;原注释写「大于10像素」与代码不符)
        if predicted_blue_end - predicted_blue_start < self.cfg_strategy["blue_min_width"]:
            return None  # 区域太小，无法点击
        
        # 转换为时间
        time_needed = frames_needed / self.cfg_data["ref_fps"]
        
        # 如果时间太长（超过5秒），可能计算有误或游戏状态变化
        if time_needed > self.cfg_strategy["wait_cap"]:
            return None
        
        return time_needed

    # ============ Game flow ============
    def wait_for_fish(self) -> Tuple[bool, bool]:
        print("  等待鱼上钩...")
        start_time = time.time()
        while self.running and not self.context.tasker.stopping:
            screenshot = self.get_screenshot()
            if screenshot is None:
                continue
            if self.detect_exclamation(screenshot):
                print("  鱼上钩! 感叹号出现")
                return True, False
            if time.time() - start_time > 25:
                return False, True
            self.delay(self.timing.wait_fish_interval)
        return False, True

    def play_minigame(self) -> bool:
        """玩钓鱼小游戏 - 预测式策略
        
        策略：
        1. 截图分析游标和区域位置
        2. 计算到达黄色/蓝色区域的时间
        3. 等待到最佳时机后点击
        4. 点击后游标重置，重复步骤1
        """
        print("  开始小游戏（预测式策略）...")
        start_time = time.time()
        click_count = 0
        total_time = self.cfg_data["minigame_seconds"]  # 后续可由识别结果更新
        # 进度条「没测到」有三种成因,不能一律当成小游戏已结束:
        #   1. 开局那几帧还没渲染出来 —— 改造前靠单帧分析耗时 0.7s+ 恰好等过了这一段,
        #      分析一提速(如 iOS 端本地算)立刻暴露,表现为每轮秒结束并误报成功;
        #   2. 中途偶发漏检(动画遮挡 / 掉帧)—— 单帧就收手会把还能救的一局判死;
        #   3. 真结束。
        # 故:见到第一帧有效之前按 (1) 等,见过之后要连续 N 帧无效才认 (3)。
        seen_valid = False
        invalid_streak = 0
        bar_wait_cap = float(self.cfg_strategy["bar_wait_cap"])
        end_invalid = int(self.cfg_strategy["end_invalid_frames"])


        while self.running and not self.context.tasker.stopping:
            current_time = time.time()
            frame = int((current_time - start_time) * 60)
            
            screenshot = self.get_screenshot()
            # if total_time is None:
            #     result = self.context.run_recognition("Reco_Minigame_Total_Time", screenshot)
            #     total_time = int(result.best_result.text)
            #     print("小游戏⏲️总时间识别结果:", total_time)
            
            # 超时检查
            if current_time - start_time > total_time:
                return True if click_count > 0 else False
            
            # 分析进度条
            bar_info = self.analyze_progress_bar(screenshot)
            if not bar_info["valid"]:
                if not seen_valid:
                    if current_time - start_time < bar_wait_cap:
                        self.delay(0.15)   # 还没画出来,等
                        continue
                    print("warn: ⚠️ 开局 %.0fs 内未检测到进度条,本轮判失败" % bar_wait_cap)
                    return False
                invalid_streak += 1
                if invalid_streak >= end_invalid:
                    return True  # 进度条持续消失,小游戏结束(可能已经钓到)
                self.delay(0.05)
                continue
            seen_valid = True
            invalid_streak = 0

            cursor_x = bar_info["cursor_x"]
            yellow_regions = bar_info["yellow_regions"]
            blue_regions = bar_info["blue_regions"]
            
            # 计算蓝色区域归0时间
            frames_to_zero = self._calculate_blue_region_zero_frame(blue_regions)
            blue_region_zero_time = (
                frames_to_zero / self.cfg_data["ref_fps"] if frames_to_zero is not None else None
            )
            
            # 2. 选择点击策略：优先黄色，其次蓝色
            target_zone = None
            wait_time = None
            
            # 检查是否应该点击黄色区域
            should_click_yellow = False
            if len(yellow_regions) > 0:
                # 检查游标是否已经越过所有黄色区域（在最后一个黄色区域的右侧）
                last_yellow_end = yellow_regions[-1][1]
                cursor_direction = self._get_cursor_direction_from_frame(frame)
                lead_px = (
                    self.cfg_data["cursor_speed"]
                    * self.cfg_timing["click_lead"]
                    * self.cfg_data["ref_fps"]
                )
                if cursor_x + lead_px < last_yellow_end:
                    should_click_yellow = True
            
            if should_click_yellow:
                # 尝试点击黄色区域（暴击）
                wait_time = self._calculate_click_timing(cursor_x, yellow_regions, frame)
                # 检查是否在蓝色区域归0前能点击
                if wait_time is not None:
                    if (
                        blue_region_zero_time is None
                        or wait_time + self.cfg_timing["click_lead"] < blue_region_zero_time
                    ):
                        target_zone = "yellow"
                    else:
                        wait_time = None  # 超时，无法点击
            
            # 如果无法点击黄色区域，尝试蓝色区域
            if target_zone is None and len(blue_regions) > 0:
                wait_time = self._calculate_blue_click_timing(cursor_x, blue_regions, frame)
                # 检查是否在蓝色区域归0前能点击
                if wait_time is not None:
                    if (
                        blue_region_zero_time is None
                        or wait_time + self.cfg_timing["click_lead"] < blue_region_zero_time
                    ):
                        target_zone = "blue"
                    else:
                        wait_time = None  # 超时，无法点击
            
            # 如果两个区域都无法点击，等待蓝色区域归0后重置
            if target_zone is None:
                if blue_region_zero_time is not None and blue_region_zero_time > 0:
                    print(f"    ⏳ 无可点击区域，等待 {blue_region_zero_time:.2f}s 后蓝色区域归0")
                    self.delay(blue_region_zero_time)
                    start_time = time.time()
                    continue
                else:
                    print("    ⚠️ 未检测到有效区域，等待...")
                    continue

            now = time.time()
            elapsed = now - current_time

            print("分析耗时: {:.3f}s".format(elapsed))
            
            # 3. 等待到最佳时机（提前补偿输入延迟） 点击后有7帧延迟，点击动作需要约0.055s 
            adjusted_wait = wait_time - elapsed - self.cfg_timing["input_comp"]
            
            if adjusted_wait > 0:
                zone_name = "黄色区" if target_zone == "yellow" else "蓝色区"
                print(f"    ⏱️ 预测 {wait_time:.3f}s 后到达{zone_name} (等待 {adjusted_wait:.3f}s)")
                self.delay(adjusted_wait)
            else:
                print(f"    ⚡ 立即点击 (预测时间: {wait_time:.3f}s)")
            
            # 4. 点击！
            self.tap(*self.coords.cast_rod)
            click_count += 1
            
            zone_emoji = "🟡" if target_zone == "yellow" else "🔵"
            zone_name = "暴击区" if target_zone == "yellow" else "蓝色区"
            cursor_direction = self._get_cursor_direction_from_frame(frame)
            print(f"    {zone_emoji} 点击{zone_name}! (游标: {cursor_x}, 帧: {frame}, 方向: {'→' if cursor_direction > 0 else '←'})")
            
            # 5. 点击后短暂等待，让游标重置到最左边
            self.delay(self.cfg_timing["cursor_reset_wait"])  # 等待游标重置
            start_time = time.time()  # 重置开始时间
        
        return False

    # ============ iOS(PlayCover)专属 ============
    # 以下三段只在 playcover 资源包下生效(self.cfg_pc_* 非 None),安卓/PC 端不会走到。

    @staticmethod
    def _bgr_to_hsv(bgr):
        """向量化 BGR→HSV(OpenCV 约定:H 0-180,S/V 0-255)。

        只为判「蓄力环有没有变绿」这一件事,不值得为此引入 cv2 —— 本模块在迁移时已经
        专门去掉过 cv2 依赖。
        """
        b = bgr[..., 0].astype(np.float32)
        g = bgr[..., 1].astype(np.float32)
        r = bgr[..., 2].astype(np.float32)
        v = np.maximum(np.maximum(b, g), r)
        mn = np.minimum(np.minimum(b, g), r)
        diff = v - mn
        s = np.where(v > 0, diff * 255.0 / np.maximum(v, 1.0), 0.0)
        h = np.zeros_like(v)
        m = diff > 0
        safe = np.maximum(diff, 1.0)
        rm = m & (v == r)
        gm = m & (v == g) & ~rm
        bm = m & (v == b) & ~rm & ~gm
        h[rm] = 60.0 * (g[rm] - b[rm]) / safe[rm]
        h[gm] = 120.0 + 60.0 * (b[gm] - r[gm]) / safe[gm]
        h[bm] = 240.0 + 60.0 * (r[bm] - g[bm]) / safe[bm]
        h = np.where(h < 0, h + 360.0, h) / 2.0
        return h, s, v

    def ensure_castable(self, tries: int = 3) -> bool:
        """确保抛竿键处于「可抛竿」态,线还在水里就先点一下收线。

        上一轮若在「线已抛出」时被打断(掉线重连、任务中途停止、鱼跑了),抛竿键会变成
        收线图标。此时再怎么按住也不会有蓄力环,而 Casting_Rod 与 Move_Forward 互为
        next/on_error 会一直空转 —— 必须先把线收回来。

        判据复用 base 的 Fishing_Already_Setsail(抛竿键模板,实测 0.99),不新增节点。
        """
        for i in range(max(1, tries)):
            shot = self.get_screenshot()
            if shot is None:
                self.delay(0.5)
                continue
            reco = self.context.run_recognition("Fishing_Already_Setsail", shot)
            if reco is not None and getattr(reco, "hit", False):
                return True
            print(f"  🪝 抛竿键处于收线态(线还在水里),点一下收线({i + 1}/{tries})")
            self.tap(*self.coords.cast_rod)
            self.delay(2.5)
        print("warn: ⚠️ 多次收线后仍未回到可抛竿态,继续尝试抛竿")
        return False

    def hold_cast_until_green(self) -> bool:
        """按住抛竿键蓄力,蓄力环变绿的瞬间松手(Perfect Cast)。

        iOS 端的绿色只在按住蓄力期间出现,空闲时环上只有白弧 —— 所以不能「等变绿再点」,
        必须 touch_down 持续按住、边按边判、见绿 touch_up。超时也松手(等效普通抛竿)。

        实测:蓄力环是抛竿键外圈半径 95~125 的一道弧,按住后颜色循环 橙→黄→绿,约
        1.5~2s 一轮;**松手即抛竿,绿色只是完美抛竿加成,不是抛竿的前提**。所以超时松手
        同样把线抛了出去,绝不能因为"没抓到绿"就重按 —— 那会把刚抛出去的线收回来。

        返回 True 表示抓到了绿。是否可以重按只看 last_hold_registered:蓄力环整轮都没
        亮过,说明这一按压根本没被游戏接收(线还没抛出,常见于结算转场未结束或鱼包已满),
        此时重按才是安全的。
        """
        cfg = self.cfg_pc_cast or _PC_CAST_DEFAULT
        cx, cy = int(cfg["ring_center_x"]), int(cfg["ring_center_y"])
        r_in, r_out = int(cfg["ring_r_inner"]), int(cfg["ring_r_outer"])
        max_hold = float(cfg["max_hold"])
        px_min = int(cfg["green_px_min"])

        x0, y0, size = cx - r_out, cy - r_out, r_out * 2
        yy, xx = np.mgrid[0:size, 0:size]
        rr2 = (xx - r_out) ** 2 + (yy - r_out) ** 2
        annulus = (rr2 >= r_in * r_in) & (rr2 <= r_out * r_out)

        print("  按住抛竿蓄力,等待变绿...")
        self.controller.post_touch_down(*self.coords.cast_rod).wait()
        t0 = time.time()
        best_seen, best_vivid, got_green, n = 0, 0, False, 0
        hues: List[float] = []          # 环亮时的色相采样,仅用于诊断/调参
        try:
            while self.running and not self.context.tasker.stopping:
                shot = self.get_screenshot()
                if shot is None:
                    # 截图失败也必须走超时出口,否则触摸悬挂、循环退不出去
                    if time.time() - t0 > max_hold:
                        print("warn: ⚠️ 蓄力期间截图持续失败,直接松手")
                        break
                    self.delay(0.1)
                    continue
                img = np.asarray(shot)
                patch = img[y0:y0 + size, x0:x0 + size, :3]
                if patch.shape[0] != size or patch.shape[1] != size:
                    print("warn: ⚠️ 截图尺寸异常,直接松手")
                    break
                H, S, V = self._bgr_to_hsv(patch)
                green = (
                    (H >= cfg["green_h_min"]) & (H <= cfg["green_h_max"])
                    & (S >= cfg["green_s_min"]) & (V >= cfg["green_v_min"]) & annulus
                )
                n = int(green.sum())
                best_seen = max(best_seen, n)
                # 环带上任何高饱和亮像素都算「蓄力环亮了」,与颜色无关 —— 这是判断
                # 「按压有没有被游戏接收」的可靠信号(环会循环变色,只看绿会误判)
                vivid_mask = (S >= cfg["green_s_min"]) & (V >= cfg["green_v_min"]) & annulus
                vivid = int(vivid_mask.sum())
                best_vivid = max(best_vivid, vivid)
                if vivid > 0:
                    hues.append(float(np.median(H[vivid_mask])))
                if n >= px_min:
                    got_green = True
                    break
                if time.time() - t0 > max_hold:
                    hue_info = ""
                    if hues:
                        hs = sorted(hues)
                        hue_info = f" 色相[{hs[0]:.0f}~{hs[-1]:.0f}]中位{hs[len(hs)//2]:.0f}"
                    print(
                        f"  ⏳ 蓄力 {max_hold}s 未抓到绿(峰值绿 {best_seen} / 环亮 {best_vivid}{hue_info}),"
                        f"松手抛竿（普通抛竿，无完美加成）"
                    )
                    break
        finally:
            self.controller.post_touch_up().wait()
        self.last_hold_best_green = best_seen
        self.last_hold_registered = best_vivid > 0
        if got_green:
            print(f"  🟢 蓄力环变绿(绿像素 {n},耗时 {time.time() - t0:.2f}s),松手抛竿!")
        return got_green

    def _tap_center_to_dismiss(self, times: int):
        """点击屏幕中央若干次,退出钓鱼态 / 关掉结算浮层。

        中央点取的是 Fishing_Minigame_Settle.target(结算点击点)—— 那本就是这套界面里
        已知安全的一点,不会误触抛竿键或方向键,不必另设坐标。
        """
        for _ in range(int(times)):
            if not (self.running and not self.context.tasker.stopping):
                return
            self.tap(*self.coords.settle)
            self.delay(0.5)

    def prepare_sell(self):
        """卖鱼前退回初始态:点几下屏幕中央关掉结算浮层 / 退出钓鱼态。

        不做「等卖鱼图标出现」那种前置判断 —— 该图标在 iOS 上是半透明白色浮在天空/海面
        之上,实测有无图标的模板得分只差 0.03,根本判不出来;而卖鱼链本身有下游 OCR
        (SellFish_Shop)把关,进没进商店由它说了算,这里只需把界面还原到可点击的状态。
        """
        cfg = self.cfg_pc_sell or _PC_SELL_DEFAULT
        print("  🧘 卖鱼前退回初始态...")
        self._tap_center_to_dismiss(cfg["center_tap_times"])
        self.delay(float(cfg["settle_wait"]))

    def sell_all_fish(self):
        print("\n==================================================")
        print("🐟💰 开始卖鱼...")

        # iOS 端先把界面还原:先关掉结算浮层,再确保抛竿键回到可抛竿态 ——
        # SellFish_Start 在 iOS 上以抛竿按钮为判据(见 playcover 覆盖层),结算刚结束
        # 那一刻它往往还没出现,链路会直接空跑。
        if self.cfg_pc_sell is not None:
            self.prepare_sell()
            self.ensure_castable()

        # Use pipeline to execute sell sequence
        # run_task 返回 Optional[TaskDetail],成败在 .status 里。旧写法整个丢弃返回值后
        # 无条件打印"卖鱼完成"并清零计数 —— 停止过程中 run_task 会立刻返回,照样报成功,
        # 而鱼其实还在包里。带 error:/warn: 前缀是 MFAAvalonia 的日志协议,裸 print 进不了 GUI。
        sell_detail = self.context.run_task("SellFish_Start")
        if sell_detail is None:
            print("warn: ⚠️ 卖鱼流程未能启动（节点缺失或正在停止），本次跳过")
            return
        if not sell_detail.status.succeeded:
            # 卖鱼没成,计数不能清零,否则下次判断"该卖了"会被推迟一整个周期
            print("error: ❌ 卖鱼流程执行失败，鱼获保留")
            return
        # status.succeeded 只说明"任务跑完了没报错",不代表鱼真卖掉了:入口节点
        # 识别不到时链路一步没走也算 succeeded。实测遇到过这种假成功 —— 计数被清零、
        # 鱼却还在包里,再钓几条就满仓,抛竿被游戏禁用、整个流程卡死。故以退栈点
        # SellFish_End 是否执行过作为"确实走完了出售"的判据。
        try:
            nodes = [getattr(n, "name", "") for n in (sell_detail.nodes or [])]
        except RuntimeError as e:
            print(f"warn: ⚠️ 读取卖鱼任务节点详情失败（任务可能已被中断）: {e}")
            return
        if "SellFish_End" not in nodes:
            print(f"error: ❌ 卖鱼未走完出售流程（节点轨迹: {nodes}），鱼获保留，下条鱼后重试")
            return

        global _total_sell_count
        _total_sell_count += 1
        self.total_sell_count = _total_sell_count
        self.fish_since_last_sell = 0
        print(f"✅ 卖鱼完成 (第 {_total_sell_count} 次)")
        print("==================================================\n")
        self.delay(1.0)

    def check_and_sell_fish(self):
        if self.fish_since_last_sell >= self.sell_interval:
            print(f"\n📦 已成功钓到 {self.fish_since_last_sell} 条鱼，触发自动卖鱼")
            self.sell_all_fish()

    def main_loop(self) -> bool:
        self.fish_count += 1
        print(f"\n[第 {self.fish_count} 次钓鱼]")
        
        # 运行 Casting_Rod pipeline，会自动执行抛竿和检测鱼上钩
        casting_result = self.context.run_task("Casting_Rod")

        # run_task 返回 Optional[TaskDetail]。旧写法 `not casting_result` 把它当 bool,
        # 再直接取 .nodes[-1].action.success —— 三处都没防护:
        #   · nodes 是惰性属性,逐个发 IPC 取详情,任一失败即 raise RuntimeError;
        #   · nodes 可能为空 → IndexError;
        #   · NodeDetail.action 是 Optional[ActionDetail] → AttributeError。
        # 这三种恰好都在"用户点了停止导致任务中断"时最容易发生,而异常穿过 ctypes 回调
        # 只会在 stderr 留一段无前缀的 traceback,GUI 里什么都看不到。
        if casting_result is None:
            print("warn: ⚠️ 抛竿任务未能启动（节点缺失或正在停止）")
            return False
        if not casting_result.status.succeeded:
            print("  等待鱼上钩超时或未检测到，重试")
            return False
        try:
            nodes = casting_result.nodes
            last_action = nodes[-1].action if nodes else None
        except RuntimeError as e:
            print(f"warn: ⚠️ 读取抛竿任务节点详情失败（任务可能已被中断）: {e}")
            return False
        if last_action is None or not last_action.success:
            print("  等待鱼上钩超时或未检测到，重试")
            return False
        
        print("  鱼上钩! 进入小游戏...")
        self.delay(self.timing.after_cast)

        success = self.play_minigame()
        if success:
            self.success_count += 1
            self.fish_since_last_sell += 1
            print(f"  ✅ 钓鱼成功 (累计成功 {self.success_count})")
        else:
            print("  ❌ 钓鱼失败")

        # 结算
        self.delay(self.timing.after_catch)
        print("  点击结算...")
        self.tap(*self.coords.settle)
        self.delay(1.0)

        self.check_and_sell_fish()

        return success

    def run(self, max_count: Optional[int] = None, max_seconds: float = 600.0) -> bool:
        self.running = True
        self.fish_count = 0
        self.success_count = 0
        print("==================================================")
        print("🎣 自动钓鱼开始 (custom action)")
        print(f"最大次数: {max_count if max_count else '无限'} | 总时长上限: {max_seconds:.0f}s")
        print("==================================================")

        # 总时长硬上界。max_count 只限轮数、不限单轮时长,而单轮里的 run_task
        # 可能长时间阻塞(Casting_Rod 与 Move_Forward 在 pipeline 里互为 next/on_error,
        # 且 PipelineTask.cpp 每次命中节点都会重置 error_handling,框架自带的
        # "error handling loop detected" 保护因此不会触发),所以必须另设 wall-clock 上界。
        # 注意:它只能在轮与轮之间生效,拦不住单次 run_task 内部的阻塞 —— 那要靠切断
        # pipeline 自环,属另一处待定改动。
        deadline = time.monotonic() + max_seconds

        try:
            while self.running and not self.context.tasker.stopping:
                if max_count and self.fish_count >= max_count:
                    break
                if time.monotonic() >= deadline:
                    print(f"warn: ⏱️ 已达总时长上限 {max_seconds:.0f}s，结束钓鱼")
                    break
                self.main_loop()
        finally:
            self.running = False
            # 兜底：结束后卖出剩余鱼获
            if self.fish_since_last_sell > 0:
                print(f"\n📦 钓鱼结束，卖出剩余 {self.fish_since_last_sell} 条鱼")
                self.sell_all_fish()
        return self.success_count > 0


@AgentServer.custom_action("FishingAction")
class FishingAction(CustomAction):
    """Entry point for Maa pipeline custom action."""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        import json
        
        # Access custom_action_param from pipeline JSON
        # It's a JSON string that needs to be parsed
        param_str = getattr(argv, 'custom_action_param', '{}')
        print("FishingAction parameters (raw):", param_str)
        param = json.loads(param_str) if isinstance(param_str, str) else param_str
        
        max_count = int(param.get("max_count", 1))
        # 不给默认值:None 表示「本次未指定」,交由 FishingBot 回落到节点 attach
        sell_interval = param.get("sell_interval")
        sell_interval = int(sell_interval) if sell_interval is not None else None
        # 总时长上限不允许缺省成"无限":默认按每轮 120s 估,业务可用 max_seconds 显式覆盖。
        max_seconds = float(param.get("max_seconds", max(120.0, max_count * 120.0)))

        bot = FishingBot(
            context=context,
            sell_interval=sell_interval
        )
        return bot.run(max_count=max_count, max_seconds=max_seconds)


@AgentServer.custom_action("HoldCastGreen")
class HoldCastGreenAction(CustomAction):
    """iOS(PlayCover)抛竿:按住蓄力,蓄力环变绿瞬间松手(Perfect Cast)。

    只由 playcover 资源包覆盖后的 Casting_Rod 以 Custom 动作调用;安卓 / PC 走的仍是
    base 的 LongPress,不经过这里。

    始终返回 True:抛竿这一步的成败由下游 Detect_Took_Bait 判定,这里返回 False 只会让
    Casting_Rod 走 on_error 进 Move_Forward,反而绕开了本该发生的上钩检测。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global _no_green_streak
        bot = FishingBot(context=context)
        bot.running = True
        cfg = bot.cfg_pc_cast or _PC_CAST_DEFAULT
        retries = int(cfg["retry_on_no_press"])
        streak_cap = int(cfg["no_green_rescue_streak"])
        # 顶层兜底:截图 / 识别 / 控制器接口抛异常不该逸出到框架核心。异常穿过 ctypes
        # 回调只会在 stderr 留一段无前缀 traceback,GUI 里什么都看不到。
        try:
            # 线还在水里时按住抛竿键不会有任何蓄力,先收线再抛
            bot.ensure_castable()
            got_green = False
            for attempt in range(max(1, retries)):
                if bot.hold_cast_until_green():
                    got_green = True
                    break
                if bot.last_hold_registered:
                    # 蓄力环亮过 = 按压已被接收,松手时线就已经抛出去了(只是没赶上绿)。
                    # 此时绝不能重按 —— 那等于把刚抛出去的线又收回来。
                    break
                if attempt < retries - 1:
                    print(f"  🔁 蓄力环全程未亮,按压未被接收,1s 后重试(第 {attempt + 2} 次)")
                    bot.delay(1.0)

            if got_green:
                _no_green_streak = 0
                return True

            # 没抓到绿。偶发一两次是正常的(绿窗口间歇出现),但**连续**多竿抓不到,
            # 已知唯一成因是鱼包已满:此时游戏不让蓄力,环照亮、颜色却停在非绿档,
            # 「环亮不亮」这个判据分辨不出来,只能靠连败计数识别。
            _no_green_streak += 1
            if _no_green_streak >= streak_cap:
                print(f"warn: 🆘 连续 {_no_green_streak} 竿未能蓄力到绿,疑似鱼包已满,尝试卖鱼自救...")
                bot.sell_all_fish()
                _no_green_streak = 0
                bot.ensure_castable()
                bot.hold_cast_until_green()
            return True
        except Exception as e:
            print(f"error: ❌ HoldCastGreen 执行异常: {e}")
            try:
                bot.controller.post_touch_up().wait()
            except Exception:
                pass
            return True
