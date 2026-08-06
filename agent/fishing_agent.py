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

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


# ==================== 基础配置 ====================

# 游标移动速度（像素/帧）
CURSOR_SPEED_PX_PER_FRAME = 4.2
# 蓝色区域每帧收缩像素数
BLUE_ZONE_SHRINK_PX_PER_FRAME = 0.83  

@dataclass
class TimingCfg:
    wait_fish_interval: float = 0.08
    after_cast: float = 0.2
    after_catch: float = 3.0
    input_delay: float = 0.055  # controller is fast; keep small buffer


@dataclass
class CoordCfg:
    cast_rod: Tuple[int, int] = (1130, 570)
    screen_center: Tuple[int, int] = (640, 360)
    progress_bar_left: int = 484
    progress_bar_right: int = 858
    minigame_area: Tuple[int, int, int, int] = (335,505,600,154)


class FishingBot:
    def __init__(
        self,
        context: Context,
        sell_interval: int = 30,
        timing: TimingCfg | None = None,
        coords: CoordCfg | None = None,
    ):
        self.context = context
        self.controller = context.tasker.controller
        self.sell_interval = sell_interval
        self.timing = timing or TimingCfg()
        self.coords = coords or CoordCfg()

        # runtime stats
        self.running = False
        self.fish_count = 0
        self.success_count = 0
        self.fish_since_last_sell = 0
        self.total_sell_count = 0

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
        
        cursor_result = self.context.run_recognition("Detect_Progress_White_Cursor", screenshot)
        blue_result = self.context.run_recognition("Detect_Progress_Blue_Zones", screenshot)
        yellow_result = self.context.run_recognition("Detect_Progress_Yellow_Zones", screenshot)

        # 三个识别节点任一"没起来"都属配置错误,而不是"这一帧没看到"。
        # 整帧判无效并只打一条日志 —— 这里是 minigame 的每帧路径,不能逐个刷屏。
        if cursor_result is None or blue_result is None or yellow_result is None:
            missing = [
                n for n, r in (
                    ("Detect_Progress_White_Cursor", cursor_result),
                    ("Detect_Progress_Blue_Zones", blue_result),
                    ("Detect_Progress_Yellow_Zones", yellow_result),
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
        cycle_frame = frame_count % 176  # 一个完整周期是176帧（右行88+左行88）
        # 0-87帧向右，88-175帧向左
        return 1 if cycle_frame < 88 else -1

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
        frames_to_zero = distance_to_center / BLUE_ZONE_SHRINK_PX_PER_FRAME
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
        frames_needed = total_distance / CURSOR_SPEED_PX_PER_FRAME
        time_needed = frames_needed / 60.0  # 假设 60 FPS
        
        # 如果时间太长（超过5秒），可能计算有误或游戏状态变化
        if time_needed > 5.0:
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
        frames_needed = total_distance / CURSOR_SPEED_PX_PER_FRAME
        
        # 计算在这段时间内，蓝色区域会收缩多少
        # 蓝色区域从两端向中心收缩，每帧收缩 BLUE_ZONE_SHRINK_PX_PER_FRAME 像素
        # 假设蓝色区域的左边界向右移动，右边界向左移动，各收缩一半
        shrink_distance = BLUE_ZONE_SHRINK_PX_PER_FRAME * frames_needed
        
        # 预测到达时蓝色区域的新位置
        predicted_blue_start = blue_start + shrink_distance
        predicted_blue_end = blue_end - shrink_distance
        
        # 检查预测的蓝色区域是否还有效（宽度大于10像素）
        if predicted_blue_end - predicted_blue_start < 5:
            return None  # 区域太小，无法点击
        
        # 转换为时间
        time_needed = frames_needed / 60.0  # 假设 60 FPS
        
        # 如果时间太长（超过5秒），可能计算有误或游戏状态变化
        if time_needed > 5.0:
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
        total_time = 17  # 默认总时间，后续从识别结果更新
        

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
                return True  # 分析失败，结束小游戏(可能已经钓到)
            
            cursor_x = bar_info["cursor_x"]
            yellow_regions = bar_info["yellow_regions"]
            blue_regions = bar_info["blue_regions"]
            
            # 计算蓝色区域归0时间
            frames_to_zero = self._calculate_blue_region_zero_frame(blue_regions)
            blue_region_zero_time = frames_to_zero / 60.0 if frames_to_zero is not None else None
            
            # 2. 选择点击策略：优先黄色，其次蓝色
            target_zone = None
            wait_time = None
            
            # 检查是否应该点击黄色区域
            should_click_yellow = False
            if len(yellow_regions) > 0:
                # 检查游标是否已经越过所有黄色区域（在最后一个黄色区域的右侧）
                last_yellow_end = yellow_regions[-1][1]
                cursor_direction = self._get_cursor_direction_from_frame(frame)
                if cursor_x + CURSOR_SPEED_PX_PER_FRAME * 0.27 * 60 < last_yellow_end:
                    should_click_yellow = True
            
            if should_click_yellow:
                # 尝试点击黄色区域（暴击）
                wait_time = self._calculate_click_timing(cursor_x, yellow_regions, frame)
                # 检查是否在蓝色区域归0前能点击
                if wait_time is not None:
                    if blue_region_zero_time is None or wait_time + 0.27 < blue_region_zero_time:
                        target_zone = "yellow"
                    else:
                        wait_time = None  # 超时，无法点击
            
            # 如果无法点击黄色区域，尝试蓝色区域
            if target_zone is None and len(blue_regions) > 0:
                wait_time = self._calculate_blue_click_timing(cursor_x, blue_regions, frame)
                # 检查是否在蓝色区域归0前能点击
                if wait_time is not None:
                    if blue_region_zero_time is None or wait_time + 0.27 < blue_region_zero_time:
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
            adjusted_wait = wait_time - elapsed - 0.045 
            
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
            self.delay(0.6)  # 等待游标重置
            start_time = time.time()  # 重置开始时间
        
        return False

    def sell_all_fish(self):
        print("\n==================================================")
        print("🐟💰 开始卖鱼...")
        
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

        self.total_sell_count += 1
        self.fish_since_last_sell = 0
        print(f"✅ 卖鱼完成 (第 {self.total_sell_count} 次)")
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
        self.tap(*self.coords.screen_center)
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
        sell_interval = int(param.get("sell_interval", 30))
        # 总时长上限不允许缺省成"无限":默认按每轮 120s 估,业务可用 max_seconds 显式覆盖。
        max_seconds = float(param.get("max_seconds", max(120.0, max_count * 120.0)))

        bot = FishingBot(
            context=context,
            sell_interval=sell_interval
        )
        return bot.run(max_count=max_count, max_seconds=max_seconds)
