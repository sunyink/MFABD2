import json
import time
import numpy as np
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context
import utils

# ==============================================================================
# 📜 智能滑动控制器 (SmartScroller - 代理节点模式)
# ==============================================================================
# [功能概述]
# 执行单次列表滑动，并通过前后 ROI区域的 视觉比对判断列表是否触底。
#
# [核心机制: 代理执行]
# 本动作不直接操作底层控制器，而是动态覆写并调用一个 MFA 原生 Swipe 节点。
# 优势：完美继承 MFA 底层的拟人化曲线、惯性保持 (end_hold) 及防封特性。
#
# [返回值逻辑]
# True  : 画面发生变化 (滑动成功) -> 列表未到底 -> Pipeline 应流转回本节点继续执行
# False : 画面静止 (确认触底)     -> 列表已到底 -> Pipeline 应触发 on_error 跳出循环
# ==============================================================================
# JSON 参数 
# "action": "Custom",
# "custom_action": "SmartSwipe",
# "custom_action_param": 
# {
#     "proxy_node": "Custom_Proxy_Swipe", // [必填] 工具人节点的名称"Custom_Proxy_Swipe"
#     "action": "Swipe",            // [必填] 预节点里或者这里写一下，别忘了。
#     "begin": [x, y, w, h],        // [必填] 滑动起点区域(需在 JSON 中预定义一个空的 Swipe 节点) //（原生）
#     "end": [x, y, w, h],          // [必填] 滑动终点区域                                   //（原生）
#     "detect_roi": [x, y, w, h],   // [必填] 视觉检测区域(用于判断画面变化)                  //（custom核心）

#     "duration": 500,              // [选填] 滑动耗时                                       //（原生）
#     "end_hold": 1000,             // [选填] 保持时间(消除滑动惯性，防止列表飞得太远)          //（原生）
#     "settle_delay": 500,          // [选填] 松手后再截图前的额外等待 (UI回弹)                //（custom）

#     "retry_times": 1,             // [选填] 确认重试次数。发现画面没动时，额外重试 N 次以排除卡顿 //（custom）
#     "threshold": 3.0              // [选填] D差异阈值 (0~255)。越小越严格，默认 3.0 过滤渲染噪点  //（custom）
# }
# ==============================================================================
# 参数详解:
# 1. detect_roi: [x, y, w, h]
#    x, y: 左上角坐标
#    w, h: 区域的【宽度】和【高度】 (不是右下角坐标！)
#    例如: [100, 100, 50, 50] 代表从 (100,100) 到 (150,150) 的区域
#
# 2. threshold: 差异阈值 (0~255)
#    算法计算的是“平均像素差异”(Mean Absolute Difference)，范围 0.0 ~ 255.0。
#    - 0.0 : 绝对静止 (两张图二进制级一致)
#    - 1.0~3.0 : 视觉静止 (允许渲染噪点、光影微变) -> 推荐默认值 3.0
#    - > 5.0 : 画面发生位移
#    注意：这与 TemplateMatch(0~1) 的逻辑完全相反！这里是越小越相似。
#
# 3. 循环次数说明
#    总执行次数 = 1 (初始滑动) + retry_times (确认重试)
#    例：设置 retry_times=2，若一直滑不动，脚本会总共执行 3 次动作后才返回 False。
# ==============================================================================

@AgentServer.custom_action("SmartSwipe")
class SmartSwipe(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            # --- 1. 参数解析 ---
            if not argv.custom_action_param:
                utils.mfaalog.error("[Py] SmartSwipe 缺少参数")
                return False
            
            if isinstance(argv.custom_action_param, dict):
                params = argv.custom_action_param
            else:
                params = json.loads(str(argv.custom_action_param).strip())

            proxy_node = params.get("proxy_node")
            begin_area = params.get("begin")
            end_area = params.get("end")
            detect_roi = params.get("detect_roi")
            
            duration = int(params.get("duration", 500))
            end_hold = int(params.get("end_hold", 1000))
            settle_delay = int(params.get("settle_delay", 500)) / 1000.0
            
            retry_times = int(params.get("retry_times", 1))
            threshold = float(params.get("threshold", 3.0))

            if not (proxy_node and begin_area and end_area and detect_roi):
                utils.mfaalog.error("[Py] SmartSwipe 缺少关键参数")
                return False

            # --- 2. 执行循环 ---
            total_attempts = 1 + retry_times
            
            for i in range(total_attempts):
                # A. 动作前采样 (同时获取到了屏幕尺寸，用于解析 detect_roi)
                img_before = context.tasker.controller.post_screencap().wait().get()
                if img_before is None:
                    utils.mfaalog.error("[Py] 截图获取失败")
                    return False

                # B. 设定坐标
                roi_before = self._crop_image(img_before, detect_roi)
                
                # C. 代理调用
                swipe_override = {
                    proxy_node: {
                        "action": "Swipe",
                        "begin": begin_area,
                        "end": end_area,
                        "duration": duration,
                        "end_hold": end_hold
                    }
                }
                # run_task 返回 Optional[TaskDetail],成败在 .status 里。
                # 旧写法把返回值整个丢弃:代理节点写错/被禁用时滑动压根没发生,而前后两帧
                # 必然一致 → diff=0 → 被判成"画面静止"→"确认触底",配置错误就这样被
                # 静默翻译成了业务结论。这里把两种情况分开报。
                swipe_detail = context.run_task(proxy_node, swipe_override)
                if swipe_detail is None:
                    utils.mfaalog.error(
                        f"[Py] ❌ 代理节点 [{proxy_node}] 未能启动"
                        f"（节点不存在/被禁用/正在停止），本次滑动未执行"
                    )
                    return False
                if not swipe_detail.status.succeeded:
                    utils.mfaalog.warning(
                        f"[Py] ⚠️ 代理节点 [{proxy_node}] 执行失败，本次滑动可能未生效"
                    )


                # D. UI 沉淀(等待动画或回弹结束)
                if settle_delay > 0:
                    time.sleep(settle_delay)

                # E. 动作后采样
                img_after = context.tasker.controller.post_screencap().wait().get()
                roi_after = self._crop_image(img_after, detect_roi)

                # F. 特征比对 (纯 Numpy 实现)(判断是否发生位移)
                diff = self._calc_diff_numpy(roi_before, roi_after)
                
                if diff >= threshold:
                    utils.mfaalog.info(f"[Py] ✅ 滑动成功 (Diff: {diff:.2f} > {threshold})")
                    return True
                else:
                    utils.mfaalog.warning(f"[Py] ⚠️ 画面静止 (Diff: {diff:.2f}) - 确认中({i+1}/{total_attempts})...")
            
            # --- 3. 触底判定 ---
            utils.mfaalog.info("[Py] 🛑 确认触底，触发 Jump Out")
            return False

        except Exception as e:
            utils.mfaalog.error(f"[Py] SmartSwipe 异常: {e}")
            return False

    # --- 辅助函数 ---
    
    def _parse_area(self, area, img_shape):
        """核心：按照框架 v5.6 规则将 ROI 坐标转换为 NumPy 支持的绝对坐标"""
        x, y, w, h = area
        h_img, w_img = img_shape[:2]

        # 1. x/y 负数表示从右/下边缘计算
        if x < 0: x += w_img
        if y < 0: y += h_img

        # 2. w/h 为负数时取绝对值，并将 (x,y) 视为右下角
        if w < 0:
            w = abs(w)
            x -= w
        if h < 0:
            h = abs(h)
            y -= h

        # 3. w/h 为 0 表示延伸至边缘
        if w == 0: w = w_img - x
        if h == 0: h = h_img - y

        # 4. 边界安全限制（防止溢出导致报错）
        x = max(0, min(int(x), w_img))
        y = max(0, min(int(y), h_img))
        w = max(1, min(int(w), w_img - x))
        h = max(1, min(int(h), h_img - y))

        return x, y, w, h

    def _crop_image(self, img, roi):
        if img is None: return None
        # 如果填写的不是 4位数组 (例如填了 string 导致异常)，做个容错防御
        if not isinstance(roi, (list, tuple)) or len(roi) != 4:
            utils.mfaalog.warning(f"[Py] detect_roi 格式错误: {roi}，退回全屏比对")
            return img 
            
        x, y, w, h = self._parse_area(roi, img.shape)
        return img[y:y+h, x:x+w]

    def _calc_diff_numpy(self, img1, img2):
        """
        纯 Numpy 实现的图像差异计算 (替代 OpenCV)
        逻辑：转灰度(可选) -> 绝对差 -> 均值
        """
        if img1 is None or img2 is None: return 0.0
        try:
            # 确保两张图尺寸一致，防报错
            if img1.shape != img2.shape:
                utils.mfaalog.warning(f"[Py] ⚠️ 图像尺寸不匹配: {img1.shape} vs {img2.shape}，强行视为画面静止以防死循环")
                return 0.0  # 如果图像大小不一致，返回报错，并且返回‘完全一致’欺骗,跳出滑动。数学上撒谎，但业务上安全。
                
            # 1. 确保是浮点数，防止 uint8 减法溢出 (2 - 5 变成 253)
            # img1 是 (H, W, 3) 的 BGR 数组
            arr1 = img1.astype(float)
            arr2 = img2.astype(float)

            # 2. 计算绝对差 |A - B|
            diff_arr = np.abs(arr1 - arr2)

            # 3. 直接求所有像素所有通道的平均值
            # (OpenCV 转灰度其实是加权平均，这里直接平均效果一样好，甚至更灵敏)
            return np.mean(diff_arr)
        except Exception as e:
            utils.mfaalog.error(f"[Py] Diff计算错误: {e}")
            return 0.0