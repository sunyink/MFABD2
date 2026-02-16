import json
import cv2
import numpy as np
from typing import Union, Optional

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
from maa.define import RectType

from utils import mfaalog

    # """
    # == HSV 形状匹配识别器 (HSV Shape Matching) ==

    # [核心功能]
    # 专用于解决“半透明”、“低饱和度”、“受背景干扰严重”的图标识别问题。
    # 例如：未激活的灰色技能图标、半透明的 UI 按钮、复杂背景上的白色水印。

    # [工作原理]
    # 1. 颜色过滤：将图像从 BGR 转为 HSV 空间，根据设定的阈值提取目标区域。
    # 2. 二值化：将提取出的区域涂黑 (0)，将背景涂白 (255)，生成一张“白底黑形状”的掩膜图。
    # 3. 模板匹配：将这张处理后的掩膜图，交给 MAA 原生的 TemplateMatch 进行形状匹配。

    # [使用前提]
    # 1. 必须准备一张【白底黑形状】的模板图片 (Template)。
    #    ⚠️ 禁止直接使用原图，请开启debug模式，利用生成的图片截取。
    # 2. 需要针对图标在不同背景下的表现，调试出合适的 HSV 阈值。（已制作了辅助开发工具：不要相信识别，请肉眼操刀调整。）
    # 3. 必须要有一个‘工具人’的节点，内部用二值化后的图片做一个普通的识图（可以多图~）。
    #
    ## 总结：此custom负责改图片，然后输出给‘工具人’节点识别后，返回结果到custom。

    # [参数说明 (JSON)]
    # - target_node (str):  必填。指定要运行的内部识别‘工具人’节点名称 (该节点需定义 template, roi 等)。
    # - lower_hsv (list):   必填。[H, S, V] 下限。例如 [0, 0, 120]。
    # - upper_hsv (list):   必填。[H, S, V] 上限。例如 [180, 50, 255]。
    # - debug (bool):       选填。默认为 False。开启后会在运行目录生成 debug_hsv_xxx.png，用于检查二值化效果。

    # [调试建议]
    # 如果识别失败，请先开启 debug: true。这回生成过滤后的全图，方便截取特征。
    # - 如果生成的图是全白的 -> 说明 HSV 范围太窄，图标漏掉了。
    # - 如果生成的图全是噪点 -> 说明 HSV 范围太宽，背景混进来了。
    # - 实践建议，对亮背景和暗背景做两个识别用or合并在一个节点，比纠结一个完美参数容易得多。
    # """
#================================================================
# {
#----------------------------------------------------------------
#        [节点 1] 入口：尝试识别普通半透明状态
#        策略：使用较宽的 HSV 范围，适配大多数情况。
#----------------------------------------------------------------
#     "FindBunny_Translucent": {
#         "recognition": "Custom",
#         "custom_recognition": "HSVShapeMatching",
#         "custom_recognition_param": {
#             "target_node": "FindBunny_Translucent_Core", // 指向核心节点
#             "lower_hsv": [0, 0, 66],   // S=0~28, V=66~255 (只要不鲜艳且不算太黑)
#             "upper_hsv": [180, 28, 255],
#             "debug": true              // 调试开关
#         },
#         "action": "Click",
#         "next": ["Next_Step_Task"],    // 成功 -> 下一步
#         "on_error": ["FindBunny_Translucent_Light"] // 失败 -> 尝试高亮方案
#     },

#----------------------------------------------------------------
#        [节点 2] 备选：尝试识别高亮/过曝状态
#        策略：当图标背景极亮时，饱和度会更低，亮度下限需提高。
#----------------------------------------------------------------
#     "FindBunny_Translucent_Light": {
#         "recognition": "Custom",
#         "custom_recognition": "HSVShapeMatching",
#         "custom_recognition_param": {
#             "target_node": "FindBunny_Translucent_Core",
#             "lower_hsv": [0, 0, 100],  // V提高到100，排除阴影
#             "upper_hsv": [180, 15, 255], // S压到15，只认极白的物体
#             "debug": true
#         },
#         "action": "Click",
#         "next": ["Next_Step_Task"],    // 成功 -> 下一步
#         "on_error": ["Skip_Or_DeepCheck"] // 都失败 -> 跳过或去深层复查
#     },

#----------------------------------------------------------------
#        [节点 3] 核心定义：模板与区域
#        注意：这个节点通常不直接作为 Task 运行，而是被上面两个节点调用。
#----------------------------------------------------------------
#     "FindBunny_Translucent_Core": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/Binary_skill3_Nol.png", // ⚠️ 必须是白底黑图！
#         "threshold": 0.6,
#         "roi": [ 966, 436, 81, 80 ], // 限制区域，提高效率
#         "green_mask": true // 忽略绿色部分（如果有需要）
#     }
# }
#================================================================


@AgentServer.custom_recognition("HSVShapeMatching")
class HSVShapeMatching(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        """
        通过 HSV 阈值将输入图像转换为白底黑形状的二值化图像，并将该处理结果传递给指定的内部识别节点进行匹配。
        
        Parameters:
            context (Context): 运行时上下文，用于调用内部识别节点（通过 context.run_recognition）。
            argv (CustomRecognition.AnalyzeArg): 分析参数封装；其中：
                - argv.custom_recognition_param (str): JSON 字符串，期望包含以下可选/必需字段：
                    - "target_node" 或兼容的旧字段 "recognition" (str, 必需)：要调用的内部识别节点名称。
                    - "lower_hsv" (list[int], 可选)：HSV 下阈值，默认 [0, 0, 120]。
                    - "upper_hsv" (list[int], 可选)：HSV 上阈值，默认 [180, 50, 255]。
                    - "debug" (bool, 可选)：为 true 时保存处理后的调试图像。
                - argv.image: 输入图像，要求为 BGR 格式的 NumPy 数组。
        
        Returns:
            CustomRecognition.AnalyzeResult | None: 
                - 当内部识别节点命中时返回包含识别框和原始识别详情的 AnalyzeResult。
                - 当未命中或发生错误时返回 `None`。
        """
        try:
            # 1. 解析参数
            raw = argv.custom_recognition_param
            if isinstance(raw, dict):
                params = raw
            else:
                params = json.loads(str(raw))
            
            # 兼容性处理：优先读取 target_node，如果没有则尝试读取 recognition
            # (防止旧配置导致参数丢失)
            recognition_node = params.get("target_node") or params.get("recognition")
            debug_mode = params.get("debug", False)
            
            if not recognition_node:
                mfaalog.error(f"[HSVShapeMatching] 参数错误: 未找到 'target_node'。当前参数: {params}")
                return None

            img = argv.image # 获取当前截图 (BGR格式)
            
            # 2. 核心逻辑：HSV 过滤与二值化
            # -------------------------------------------------
            hsv_img = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            
            # 获取阈值，提供一组针对“半透明白色物体”的默认值
            lower_hsv = np.array(params.get("lower_hsv", [0, 0, 120]))
            upper_hsv = np.array(params.get("upper_hsv", [180, 50, 255]))

            # 生成掩码 (Mask): 在范围内的像素=255(白)，不在=0(黑)
            mask = cv2.inRange(hsv_img, lower_hsv, upper_hsv)
            
            # 图像反转 -> 生成【白底黑图】
            # 因为 MAA 的 TemplateMatch 对白底黑线稿的兼容性通常更好
            processed_img = np.full_like(img, 255) # 创建全白底图
            processed_img[mask > 0] = [0, 0, 0]    # 将掩码区域（目标）涂黑

            # 3. 调试输出
            # -------------------------------------------------
            if debug_mode:
                import time
                import os
                debug_dir = "debug_images"
                if not os.path.exists(debug_dir):
                     try:  
                        os.makedirs(debug_dir, exist_ok=True)  
                     except OSError as e:  
                        mfaalog.warning(f"[HSVShapeMatching] 创建调试目录失败: {e}") 

                # 文件名格式: debug_hsv_[节点名]_[时间戳_毫秒].png
                # 例如: debug_hsv_FindBunny_Core_1725123456_789.png
                timestamp = f"{time.time():.3f}".replace('.', '_')
                safe_node_name = recognition_node.replace('/', '_').replace('\\', '_')
                
                filename = f"{debug_dir}/debug_hsv_{safe_node_name}_{timestamp}.png"
                
                cv2.imwrite(filename, processed_img)
                mfaalog.info(f"[HSVShapeMatching] 调试模式已开启，处理图已保存至: {filename}")

            # 4. 移交识别
            # -------------------------------------------------
            # 注意：这里传进去的是【黑色的图标形状 + 纯白背景】
            # 你的 template 图片也必须是【黑色的图标形状 + 纯白背景】
            reco_detail = context.run_recognition(recognition_node, processed_img)

            if reco_detail and reco_detail.hit:
                # 命中目标，返回结果
                # (可选：打印匹配度，方便微调 threshold)
                if reco_detail.best_result:
                     mfaalog.debug(f"[HSVShapeMatching] 命中目标: {recognition_node}, Score: {reco_detail.best_result.score:.4f}")
                
                return CustomRecognition.AnalyzeResult(
                    box=reco_detail.box,
                    detail=reco_detail.raw_detail
                )
            
            return None

        except Exception as e:
            mfaalog.error(f"[HSVShapeMatching] 执行异常: {e}")
            return None