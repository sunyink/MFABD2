import json
import re
import time
import os
import traceback
import numpy as np
from collections import deque
from typing import Union, Optional
from PIL import Image, ImageFilter

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
from maa.define import RectType

from utils import mfaalog
from .rdd_sampler import RddSampler
from .rdd_hsv_rescue import (
    DIRECTIONS,
    boxes_agree,
    channel_cutpoints,
    direction_deltas,
    is_strict_mask,
    lineage_parent,
    neighbor_states,
    normalize_rescue_config,
    select_stable_winner,
    sort_parents,
    strict_profile,
)


# ================================================================
# == 模块级 HSV 工具函数（HSVShapeMatching / RedDotDetector 共用）==
# ================================================================

def _map_h(h_cv: int, is_upper: bool = False) -> int:
    """OpenCV H(0-180) → Pillow H(0-255)，下界 floor，上界 ceil。"""
    val = h_cv * (255.0 / 180.0)
    return min(255, int(np.ceil(val))) if is_upper else max(0, int(np.floor(val)))


def _compute_hsv_mask(hsv_np: np.ndarray, ranges: list) -> np.ndarray:
    """多组 HSV 阈值 OR 合并，返回 bool mask。"""
    combined = np.zeros(hsv_np.shape[:2], dtype=bool)
    for rng in ranges:
        lo = rng.get("lower") or rng.get("lower_hsv")
        hi = rng.get("upper") or rng.get("upper_hsv")
        lower_pil = np.array([_map_h(lo[0], False), lo[1], lo[2]])
        upper_pil = np.array([_map_h(hi[0], True),  hi[1], hi[2]])
        combined |= np.all((hsv_np >= lower_pil) & (hsv_np <= upper_pil), axis=-1)
    return combined


def _label_blobs(mask: np.ndarray):
    """
    BFS 连通域标注（4-邻域），无外部依赖。
    返回 (labeled_array, num_labels)，labeled_array[y,x] 为 1-based 标签，0 表示背景。
    """
    h, w = mask.shape
    labeled = np.zeros((h, w), dtype=np.int32)
    label = 0
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or labeled[sy, sx]:
                continue
            label += 1
            queue = deque([(sy, sx)])
            labeled[sy, sx] = label
            while queue:
                y, x = queue.popleft()
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labeled[ny, nx]:
                        labeled[ny, nx] = label
                        queue.append((ny, nx))
    return labeled, label


# ================================================================
# == HSV 形状匹配识别器 (HSV Shape Matching) ==
#
# [核心功能]
# 先对截图做 HSV 颜色过滤 + 二值化，再将处理后的图像交给任意 MAA 识别节点执行。
# 适用于半透明图标、低饱和度 UI 元素、受复杂背景干扰的目标。
#
# [工作流程]
# 1. 固定一帧截图 (argv.image)，全程复用，不重新截图。
# 2. 按 HSV 阈值过滤目标像素，生成"白底黑形状"掩膜图。
#    支持多组阈值 OR 合并，应对同一画面明/暗背景切换。
# 3. 将掩膜图交给"核心节点"执行 MAA 原生识别（TemplateMatch / OCR 等均可）。
# 4. 可选 edge_assist 模式：HSV 掩膜识别失败后，追加边缘 AND 掩膜再尝试一次，
#    用于剥离颜色与目标相近的平坦背景噪声。
#
# [参数说明]
#
# --- 方案 A: hsv_ranges（多组阈值 → 同一节点）---
#   target_node    (str)   必填。核心识别节点名称。
#   hsv_ranges     (list)  必填。[{lower:[H,S,V], upper:[H,S,V]}, ...]，多组 OR 合并。
#
# --- 方案 B: hsv_map（每组阈值 → 各自节点）---
#   hsv_map        (dict)  必填。{node_name: {lower,upper} 或 [{...},...]}，按序尝试。
#
# --- 旧格式（单组，兼容保留）---
#   target_node    (str)   必填。
#   lower_hsv      (list)  必填。[H, S, V] 下限。
#   upper_hsv      (list)  必填。[H, S, V] 上限。
#
# --- 通用可选参数 ---
#   edge_assist    (bool)  默认 false。HSV 失败后，追加边缘 AND 掩膜再识别一次。
#   edge_threshold (int)   默认 15。边缘强度阈值（0-255），越低越敏感。
#   debug          (bool)  默认 false。保存各阶段中间图，日志输出像素覆盖率。
#
# [HSV 坐标系]
#   配置使用 OpenCV 标准：H 0-180，S 0-255，V 0-255。
#   代码内部自动映射到 Pillow 标准（H 0-255），用户无需关心。
#   注意：红色在 HSV 中跨越 H=0，建议拆成两组阈值（如 [170,S,V]~[180,S,V]
#   和 [0,S,V]~[10,S,V]）通过多组 OR 完整覆盖，而非强行写 low > high。
#
# [调试说明]
#   debug:true 时，每次识别生成以下文件：
#     debug_{node}_{ts}_1_hsv.png    → HSV 掩膜结果（白底黑形状）
#     debug_global_{ts}_2_edge.png   → 边缘掩膜（仅 edge_assist 模式）
#     debug_{node}_{ts}_3_and.png    → AND 结果（仅 edge_assist 模式）
#   日志同步输出各阶段覆盖率，辅助判断阈值方向：
#     覆盖率 ≈  0% → HSV 范围太窄，目标被漏掉
#     覆盖率 > 50% → HSV 范围太宽，背景混入
#
# ================================================================
#
# [使用示例 A] hsv_ranges —— 多组阈值 OR 合并，对接同一个核心节点
#   适合场景：图标在明/暗背景下外观相似，同一套模板可以匹配两种情况。
#
# {
#     "FindIcon": {
#         "recognition": "Custom",
#         "custom_recognition": "HSVShapeMatching",
#         "custom_recognition_param": {
#             "target_node": "FindIcon_Core",
#             "hsv_ranges": [
#                 {"lower": [0,  0,  40], "upper": [180, 30, 180]},
#                 {"lower": [0,  0, 180], "upper": [180, 15, 255]}
#             ],
#             "edge_assist": true,
#             "edge_threshold": 15,
#             "debug": true
#         },
#         "action": "Click",
#         "next": ["NextTask"]
#     },
#     "FindIcon_Core": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/icon.png",
#         "threshold": 0.6,
#         "roi": [100, 200, 80, 80]
#     }
# }
#
# [使用示例 B] hsv_map —— 每组阈值对接不同的核心节点
#   适合场景：明/暗背景下图标形状或细节差异较大，需要不同模板或不同 ROI。
#   hsv_map 中的节点按声明顺序逐一尝试，第一个命中即返回。
#
# {
#     "FindIcon_Adaptive": {
#         "recognition": "Custom",
#         "custom_recognition": "HSVShapeMatching",
#         "custom_recognition_param": {
#             "hsv_map": {
#                 "FindIcon_Core_Dark":  {"lower": [0, 0, 40],  "upper": [180, 30, 180]},
#                 "FindIcon_Core_Light": {"lower": [0, 0, 180], "upper": [180, 15, 255]}
#             },
#             "debug": false
#         },
#         "action": "Click",
#         "next": ["NextTask"]
#     },
#     "FindIcon_Core_Dark": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/icon_dark_bg.png",
#         "threshold": 0.6,
#         "roi": [100, 200, 80, 80]
#     },
#     "FindIcon_Core_Light": {
#         "recognition": "TemplateMatch",
#         "template": "Binary/icon_light_bg.png",
#         "threshold": 0.55,
#         "roi": [100, 200, 80, 80]
#     }
# }
#
# ================================================================


@AgentServer.custom_recognition("HSVShapeMatching")
class HSVShapeMatching(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        """
        HSV 形状匹配识别器（Pillow 无 OpenCV 依赖版，兼容 Windows ARM64）

        工作流程：
        1. 固定截图 argv.image，全程复用，不重复截图。
        2. 按 HSV 阈值（支持多组 OR）生成白底黑形状掩膜图，交给目标节点识别。
        3. 若 edge_assist=true，HSV 失败后追加边缘 AND 掩膜再试一次。

        参数格式与完整示例见模块顶部注释块。
        """
        try:
            # 0. 固定截图，全程复用
            original_bgr = argv.image
            ts = f"{time.time():.3f}".replace('.', '_')

            # 1. 解析参数
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))

            debug_mode     = params.get("debug", False)
            edge_assist    = params.get("edge_assist", False)
            edge_threshold = params.get("edge_threshold", 15)

            # 2. BGR → PIL → HSV（只做一次）
            pil_img = Image.fromarray(original_bgr[..., ::-1])  # BGR → RGB → PIL
            hsv_np  = np.array(pil_img.convert("HSV"))

            # 3. 解析任务列表 [(node_name, [ranges])]
            tasks = self._parse_tasks(params)
            if not tasks:
                mfaalog.error("[HSVShapeMatching] 参数错误：未找到有效节点配置")
                return None

            # 4. 预计算 edge_mask（全局只算一次）
            edge_mask = None
            if edge_assist:
                edge_mask = self._compute_edge_mask(pil_img, edge_threshold)
                if debug_mode:
                    edge_vis = self._mask_to_bgr(original_bgr, edge_mask)
                    self._save_debug(edge_vis, "global", ts, "2_edge", edge_mask)

            # 5. 逐节点尝试
            for node_name, ranges in tasks:
                # 生成 HSV 掩膜（多范围 OR）
                hsv_mask      = _compute_hsv_mask(hsv_np, ranges)
                processed_hsv = self._mask_to_bgr(original_bgr, hsv_mask)

                if debug_mode:
                    self._save_debug(processed_hsv, node_name, ts, "1_hsv", hsv_mask)

                # 尝试 HSV 掩膜识别
                result = self._try_recognition(context, node_name, processed_hsv, "HSV")
                if result:
                    return result

                # edge_assist 升级：AND 掩膜
                if edge_assist and edge_mask is not None:
                    and_mask      = hsv_mask & edge_mask
                    processed_and = self._mask_to_bgr(original_bgr, and_mask)
                    if debug_mode:
                        self._save_debug(processed_and, node_name, ts, "3_and", and_mask)
                    result = self._try_recognition(context, node_name, processed_and, "AND")
                    if result:
                        return result

            return None

        except Exception:
            mfaalog.error(f"[HSVShapeMatching] 执行异常:\n{traceback.format_exc()}")
            return None

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _parse_tasks(self, params: dict) -> list:
        """解析参数，返回 [(node_name, [ranges])] 列表。"""
        if "hsv_map" in params:
            # 方案 B
            return [
                (node, spec if isinstance(spec, list) else [spec])
                for node, spec in params["hsv_map"].items()
            ]

        # 方案 A 或旧格式
        node = params.get("target_node") or params.get("recognition")
        if not node:
            return []
        if "hsv_ranges" in params:
            ranges = params["hsv_ranges"]
        else:
            ranges = [{"lower": params.get("lower_hsv", [0, 0, 120]),
                       "upper": params.get("upper_hsv", [180, 50, 255])}]
        return [(node, ranges)]

    def _compute_edge_mask(self, pil_img: Image.Image, threshold: int) -> np.ndarray:
        """基于 Pillow FIND_EDGES 生成边缘 bool mask。"""
        edge_np = np.array(pil_img.convert("L").filter(ImageFilter.FIND_EDGES))
        return edge_np > threshold

    def _mask_to_bgr(self, original_bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """将 mask 渲染为白底黑形状的 BGR 图（目标区域=黑，其余=白）。"""
        result = np.full_like(original_bgr, 255)
        result[mask] = [0, 0, 0]
        return result

    def _save_debug(self, bgr_img: np.ndarray, node_name: str, ts: str,
                    stage: str, mask: np.ndarray) -> None:
        """保存调试图，并在日志打印像素覆盖率。"""
        hit = int(np.sum(mask))
        pct = hit / mask.size * 100
        try:
            debug_dir = "debug_images"
            os.makedirs(debug_dir, exist_ok=True)
            safe_node = re.sub(r'[<>:"/\\|?*]', '_', node_name)
            filename  = f"{debug_dir}/debug_{safe_node}_{ts}_{stage}.png"
            Image.fromarray(bgr_img[..., ::-1]).save(filename)
            mfaalog.info(f"[HSVShapeMatching] [{stage}] {node_name} 覆盖 {hit}px ({pct:.1f}%) → {filename}")
        except Exception as e:
            mfaalog.warning(f"[HSVShapeMatching] [{stage}] {node_name} 覆盖 {hit}px ({pct:.1f}%) | 调试图保存失败: {e}")

    def _try_recognition(self, context: Context, node_name: str,
                         processed_bgr: np.ndarray, stage: str) -> Optional[CustomRecognition.AnalyzeResult]:
        """调用 MAA 识别节点，命中返回 AnalyzeResult，否则返回 None。"""
        detail = context.run_recognition(node_name, processed_bgr)
        if detail and detail.hit:
            mfaalog.debug(f"[HSVShapeMatching] [{stage}] 命中: {node_name}")
            return CustomRecognition.AnalyzeResult(box=detail.box, detail=detail.raw_detail)
        return None


# ================================================================
# == 红点感叹号识别器 (Red Dot Detector) ==
#
# [核心功能]
# 在任意背景下稳健识别游戏 UI 中的红色通知点（内含白色感叹号）。
# 不依赖 TemplateMatch，通过颜色拓扑 + 垂直投影分析确认感叹号结构。
#
# [两种模式]
#   独立模式：节点自带完整参数，直接执行。
#   预设模式：节点只写 {"preset": "节点名"}，复用预设节点参数；命中坐标自动加回 roi 偏移。
#             调用者节点名 + ROI 会透传给预设节点，用于失败截图命名（见可观测性）。
#
# [识别原理 —— 置信度加权模型 v2]（真假样本论证见 docs/RedDotDetector_打分模型.md）
#   先做"筛选六步"，任一步不过即淘汰；筛到最后的封闭区(enclosed，被红包围的非红芯)
#   才进入"打分四项"，每项算 0~1，加权求和 ≥ sc_min_conf 即命中。
#
#   筛选六步：框选 roi → 红掩膜(HSV) → 连块 → 面积(flt_area) → 长宽比(flt_aspect) → 取封闭内部区。
#
#   打分四项（权重见模块常量 _SC_W_*；硬不变量 max(单项权重)=0.45 < 阈值0.55，
#             故命中必须"竖长+偏白"两可靠项同时背书，单项满分越不过阈值）：
#     vert (竖长 0.45)  内部高/宽：高瘦感叹号→1，矮胖杂块→0。抗模糊主轴之一。
#     white(偏白 0.45)  白芯白度中位数 − 红环白度中位数；白笔画恒比红环白，杂块不白。
#                       抗模糊主轴之一，真假最干净的金线。
#     gap  (断层 0.06)  三段诚实断层 = 深度 × 细窄度。感叹号是上段/凹陷/下段三段，
#                       凹陷高度纳入计算(占总高超 _SC_GAP_MAX_RATIO=0.40 即视为两块不相干→0)；
#                       因 adb 模糊会填平真缝/在杂红造假缝，断层不可靠，权重压到最低，仅清晰帧奖励。
#     cent (居中 0.04)  内部水平中心 vs 红块中心，弱分离，微调。
#   conf 与各分项写入 detail；金字塔回调结构见下方 [可观测性]。
#
# [为什么 v2 这样改]（一句话）
#   旧版断层权重 0.45 且"有一根空行就满分"，被 adb 模糊在杂红里造的假缝顶满 → 误命中；
#   且断层是反指标(真货模糊帧断层=0、假货断层=1)。v2 改用抗模糊的竖长+偏白做主轴，
#   断层降权 + 纳入凹陷高度变诚实，真假分离从"假分>真分"翻转为"真0.63+ / 假0.05"。
#
# [参数说明]（HSV 坐标系：OpenCV 标准 H 0-180 / S,V 0-255，内部自动映射 Pillow）
#   两级命名：flt_ = 筛选阶段，sc_ = 打分阶段。下面一律用新名，旧名见本段末尾对照表。
#   flt_red_hsv    (list)  红色 HSV 范围 [{lower:[H,S,V], upper:[H,S,V]}, ...]，H 跨 0 拆两组 OR。
#   flt_area       (list)  红色 blob 面积范围 [min, max]，默认 [30, 1200]。
#   flt_aspect     (list)  红块外接框 h/w 范围 [min, max]，默认 [0.6, 1.6](v3 形状闸门)。
#                          真货实测紧聚 0.92~1.10 且抗模糊；横条(≈0.5)/实心柱(≈2.1)杂红在圈外。
#                          仅新名无旧名；设 [0, 99] 等于禁用。
#   sc_min_conf    (float) 命中阈值(0-1)，默认 0.55(v2 量纲；真货约 0.63+，留 0.08 余量)；
#                          必须 > 最大单项权重 0.45，否则单项就能越阈值，破坏"两项背书"。
#                          大 ROI 泛找可调高到 0.58。
#   sc_gap_ratio   (float) 仅用于 detail 里 gap 的"是否成双段"标注，不再作命中门槛。
#   preset         (str)   预设节点名（预设模式）。
#   flt_hsv_rescue (object) 严格 HSV 拓扑救援。mode=off/shadow/active；仅 baseline
#                          最终卡 aspect 时提高 S/V lower，严格子集经 lineage+跨档稳定
#                          后才可命中。默认 off；预算/歧义/异常均 fail closed。
#
#   旧名对照(存量节点仍可用，由 _PARAM_ALIAS 翻译；同时写则新名优先)：
#       flt_red_hsv ← hsv_ranges      flt_area     ← red_area
#       sc_min_conf ← min_confidence  sc_gap_ratio ← gap_ratio
#   ⚠️ 内部变量、detail 与样本台账落盘用的仍是旧名(hsv_ranges / red_area / ...)——
#      那是数据键不是配置字段，离线回放工具按旧名读取，改不得。看日志时按此对应。
#   注：旧的 inner_v_min / inner_s_max 已弃用(绝对亮度阈值在模糊下会掉崖)，若残留会被忽略。
#
# [可观测性 —— 金字塔回调 + vision 调试图]
#   detail 采用金字塔结构(命中/失败都有)，三层递进：
#     第1层  result   = hit / miss
#     第2层  阶段      = 筛选 / 打分
#     第3层  · 阶段=筛选 → {卡在: 哪步, 数据: 筛选计数}
#            · 阶段=打分 → {总分, 阈值, 通过, 明细:[每项 值/权重/贡献]}
#       「贡献」= 值×权重，调参时看这一列，一眼定位谁把分顶上去/谁拖了后腿。
#     随 MAA 识别记录进入日志分析工具(MaaLogAnalyzer / MaaLogs)，图没了也能复盘。
#     · mfaalog.warning 输出一行精简摘要(上 UI)；print 输出拼贴明细(仅进 txt 日志)。
#
#   vision 调试图(原生回显)：受 save_draw 门控(RDD_DRAW=0 可强制关)——
#     · 自定义识别的 C API 回调只有 box+detail 两个输出通道，没有注入 draw 的接口，
#       Custom 节点自身永远没有原生 vision 图；自建图对不上 reco_id，日志工具不认。
#     · 故识别结束后借一次内置 ColorMatch(method 40，同 flt_red_hsv/flt_area min)在
#       整屏原图上重跑红色过滤，由框架原生画图落盘：绿框=ROI，红字 R:[box]=红色
#       blob 包围框(与本识别器返回的 box 是同一个红块)。
#     · 原生图带 reco_id，框架自动存入 <log_dir>/vision(VSCode 调试时即扩展目录)，
#       MaaLogAnalyzer / MaaLogs 原生显示，与本节点识别记录相邻，可对照金字塔 detail。
#     · 注意：回显图只体现"红色在哪"(ColorMatch 语义)，被打分拒掉的杂红同样会画框；
#       命中/未命中都画(误命中现场需复盘)。
#
#   失败小图(常驻，无开关)：落盘 roi_crop / red_mask / inner 三张(各几百字节)到
#     <log_dir>/RedDotDetector/，文件名 = 节点名+ROI(同检测点覆盖)，时间节流防自循环刷屏
#     (默认 2s，RDD_DUMP_INTERVAL 可调)。RDD_DEBUG_DIR 可强制指定日志根目录。
#
#   样本采集(语料即回归集，见 rdd_sampler.py)：命中/未命中都把小图 + 完整识别信息
#     (conf 四项分解/proj/红块 aspect·fill/生效参数)落成累积语料：
#     <log_dir>/RedDotDetector_samples/ 下唯一命名小图 + samples.jsonl.log(一行一事件，
#     .log 后缀是为了能被 UI"导出日志"收走，理由见 rdd_sampler.py 头注)。
#     整个文件夹拿走即可离线回放定标(v3)。RDD_SAMPLE=off/fail/all(默认 all，env 穿透
#     运行侧配置)；RDD_SAMPLE_DIR 指定落盘目录(VSCode 调试 log_dir 被重定向时用)；
#     同检测点默认 1800s 采一张(RDD_SAMPLE_INTERVAL 可调)+ 画面不变去重。
#
# [一句话调参口诀]（对照 detail：阶段→卡在/明细）
#   筛选·红掩膜  → HSV 没框到红色：降低 S/V 下限 / 校正 roi
#   筛选·面积    → 面积不在 flt_area：多半 min 太大
#   筛选·长宽比  → 红块 h/w 出圈(看 aspect_rej)：横条/竖柱杂红=正常拒；
#                  连片真货由 flt_hsv_rescue 严格 HSV 搜索拆开；不要放宽 flt_aspect
#   筛选·内部    → 红块内无封闭非红区(无感叹号轮廓)：roi 偏移 / 红圈破损 / 被模糊填满
#   打分        → 看「明细」贡献列：竖长/偏白低=非感叹号(正常拒)；真货被拒才降 sc_min_conf
#
# ================================================================
#
# [示例 A] 独立模式
# {
#     "CheckRedDot": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {
#             "flt_red_hsv": [
#                 {"lower": [0,   140, 120], "upper": [12,  255, 255]},
#                 {"lower": [165, 140, 120], "upper": [180, 255, 255]}
#             ],
#             "flt_area": [30, 1200], "sc_min_conf": 0.25
#         },
#         "roi": [950, 100, 40, 600], "action": "Click", "next": ["NextTask"]
#     }
# }
#
# [示例 B] 预设模式（多面板共用一套参数，仅 roi 不同）
# {
#     "RedDot_Preset": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {
#             "flt_red_hsv": [
#                 {"lower": [0,   140, 120], "upper": [12,  255, 255]},
#                 {"lower": [165, 140, 120], "upper": [180, 255, 255]}
#             ],
#             "flt_area": [30, 1200], "sc_min_conf": 0.25
#         }
#     },
#     "CheckPanel_A": {
#         "recognition": "Custom",
#         "custom_recognition": "RedDotDetector",
#         "custom_recognition_param": {"preset": "RedDot_Preset"},
#         "roi": [640, 616, 17, 16], "action": "Click", "next": ["AfterClick"]
#     }
# }
#
# ================================================================
# [调参指南] 识别原理与逐步排查
# ================================================================
#
# 失败时自动落盘（无需任何开关，覆盖写入，时间节流防刷屏）：
#   rdd_*_roi_crop.png  → 代码实际处理的裁剪区域
#   rdd_*_red_mask.png  → 红色掩膜（黑=红色，白=非红）
#   rdd_*_inner.png     → 最佳候选 blob 的封闭内部区（黑=enclosed）
# 文件位置：debug/RedDotDetector/ 目录（maa.log 同级，RDD_DEBUG_DIR 可强制指定）。
# 文件名以"节点名+ROI"为 key，同检测点重复失败直接覆盖。
#
# 失败原因结构化写入 detail，通过 MaaLogs 工具可复盘（图没了也能看数据）：
#   detail.阶段 / 卡在     筛选(red_mask / area / aspect / interior)或打分
#   detail.说明            具体提示与修正方向
#   detail.stat.conf       最高候选置信分（confidence 阶段专用）
#   detail.stat.parts      各分项得分 {gap, vert, white, cent}
#   detail.stat.proj       垂直投影数组（按行的封闭区像素数，用于判断双段）
#   detail.stat.gap        断层数值 {row, val, peak, ratio, above_nz, has_below}
#
# ────────────────────────────────────────────────────────────────
# 阶段 1  ROI 裁剪
# ────────────────────────────────────────────────────────────────
# 参数：任务 JSON 的顶层 roi 字段（不在 custom_recognition_param 里）
# 输出：rdd_*_roi_crop.png  ← 代码实际处理的像素区域
#
# 排查：打开 roi_crop.png，红点必须完整在图内。
#       若图里没有红点 → 坐标填错了，对着游戏原图重新量取。
#       建议 roi 比红点略大 2-4px；精准 ROI 还能抑制大面积杂红误判。
#
# ────────────────────────────────────────────────────────────────
# 阶段 2  HSV 过滤 → 红色掩膜（detail.stage = "red_mask"）
# ────────────────────────────────────────────────────────────────
# 参数：flt_red_hsv（H 0-180 / S 0-255 / V 0-255，OpenCV 坐标系）
# 输出：rdd_*_red_mask.png  ← 黑=检测为红，白=非红
#
# 调参方法：
#   在游戏截图上拾取红点边缘 3-4 个像素，记录 RGB → 转 HSV；
#   各分量取最值后留 10-20 的余量作阈值范围。
#   注意：游戏红色常跨越 H=0（H 在 170-180 和 0-10 各有一段），
#   须拆成两组 OR 合并；单组 lower > upper 无效。
#   红色连片（红点与其他红色 UI 连成一块）不影响识别：感叹号的拓扑关系不变。
#
# 判断（看 red_mask.png）：
#   全白             → flt_red_hsv 没覆盖到实际红色，降 S/V 下限
#   大片黑色（背景黑）→ flt_red_hsv 太宽，提高 S 或 V 下限
#   菱形轮廓完整黑色 → 正常，进入下一阶段
#
# ────────────────────────────────────────────────────────────────
# 阶段 3  连通域面积筛选（detail.stage = "area"）
# ────────────────────────────────────────────────────────────────
# 参数：flt_area [min, max]
# 数据：detail.stat.n_blobs（总连通域数）、detail.stat.area_pass（通过面积的数量）
#
# 面积估算：菱形面积 ≈ 对角线² / 2。16px 菱形 ≈ 128px，10px 菱形 ≈ 50px。
# 建议 min=30，max=1200；场景有大面积红色 UI 元素时适当调小 max。
# area_pass=0 → 所有 blob 被过滤，多半 min 太大。
#
# ────────────────────────────────────────────────────────────────
# 阶段 4  红块 h/w 筛选（detail.卡在 = "aspect"）
# ────────────────────────────────────────────────────────────────
# 参数：flt_aspect [min,max]，默认 [0.6,1.6]。
# 横条/竖柱正常拒绝；若所有面积候选均卡在此处，可由 flt_hsv_rescue 在严格子集、
# lineage、跨档稳定与预算约束下尝试拆开连片。禁止直接放宽 flt_aspect。
#
# ────────────────────────────────────────────────────────────────
# 阶段 5  拓扑封闭取内部区（detail.卡在 = "interior"）
# ────────────────────────────────────────────────────────────────
# 输出：rdd_*_inner.png  ← 黑=enclosed（被红色真正包围的非红像素）
#
# 原理：对 blob 包围框内的非红像素做连通域标注，凡能从矩形边框触达的
# 连通域视为"外侧背景"，其余即 enclosed。此步骤不卡任何绝对亮度——
# 被模糊压暗的感叹号白色同样会被收进来，"能不能区分"是后续打分的事。
# 旧版 inner_v_min / inner_s_max 绝对阈值在此移除，掉崖问题由此解决。
#
# 卡住原因（max_inner_px = 0，inner.png 全白）：
#   · roi 偏移或太小，红圈不完整，边框漏到红圈外侧
#   · 极度模糊时红色"填满"感叹号缝隙，封闭区消失（物理边界，无法靠参数解决）
#
# ────────────────────────────────────────────────────────────────
# 阶段 6  置信度加权评分 v2（detail.阶段 = "打分"）
# ────────────────────────────────────────────────────────────────
# 参数：sc_min_conf（默认 0.55；必须 > 最大单项权重 0.45 → 命中需两项背书）
# 数据：detail.打分 = {总分, 阈值, 通过, 明细:[每项 值/权重/贡献]}
#
# 四个分项（权重在模块常量 _SC_W_* 可直接调整；完整真假样本论证见
#           docs/RedDotDetector_打分模型.md）：
#
#   vert (竖长 0.45)  内部高/宽 clip(h/w−1,0,1)。高瘦感叹号 h/w≥2→1，矮胖杂块≈1→0。
#                     抗模糊主轴之一(模糊不改变长宽比)。
#   white(偏白 0.45)  内部白度中位数 − 红环白度中位数，whiteness=(V/255)(1−S/255)。
#                     白笔画恒比红环白(实测真货 0.30+)，杂块内部不白(实测假货 0.04)，
#                     抗模糊主轴之一，真假最干净的金线。
#   gap  (断层 0.06)  三段诚实断层 = 深度 × 细窄度。
#                     深度  = 1 − 谷底像素/最高行像素(谷有多空)；
#                     细窄度= 1 − (凹陷连续高度/内部总高)/0.40(缝越宽越不像真缝→0)。
#                     感叹号是上段/凹陷/下段三段，旧版只看"有无一根空行"把凹陷当点不当段，
#                     被 adb 模糊在杂红里造的假缝顶满 → 误命中；故降权到最低，仅清晰帧奖励。
#   cent (居中 0.04)  内部水平居中程度，弱分离，微调。
#
# 真假样本实测（proj 来自真机，详见 docs）：
#   true1(adb清晰)：vert1.0 white0.30 gap0.58 → conf 0.66  命中
#   true2(adb模糊)：vert1.0 white0.32 gap0    → conf 0.63  命中(断层失效，竖长+偏白兜底)
#   true3(PC)     ：vert1.0 white0.34 gap0    → conf 0.64  命中
#   假1(adb杂红)  ：vert0   white0.04 gap0    → conf 0.05  拒(凹陷7/11→细窄度0)
#   高瘦白假货(推演)：vert1.0 white0.04 cent1 → conf 0.51  拒(偏白补不上，差0.04)
#   真货 0.63~0.66 / 假货 ≤0.05，中间 0.55 阈值留足两侧余量。
#
# 调参方法（看 detail.打分.明细 的"贡献"列）：
#   竖长 或 偏白 贡献低 → 非感叹号，正常拒，不要为它降阈值；
#   真货被误拒(两项贡献都不低却差一点) → 适度降 sc_min_conf；
#   大 ROI 泛找误判 → 升 sc_min_conf 到 0.58。
#
# ────────────────────────────────────────────────────────────────
# 物理边界说明
# ────────────────────────────────────────────────────────────────
# 红色完全填满感叹号缝隙时：封闭区为空，识别无解，任何参数均无效(卡在筛选·内部)。
# 此前的模糊过渡区，竖长+偏白仍稳定可分，不再依赖已失效的断层。
#
# ────────────────────────────────────────────────────────────────
# 一句话调参口诀（对照 detail.阶段）
# ────────────────────────────────────────────────────────────────
# 筛选·红掩膜(red_mask)  → red_mask 全白：降 flt_red_hsv S/V 下限 / 校 roi
# 筛选·面积(area)        → area_pass=0：降 flt_area min（或升 max）
# 筛选·长宽比(aspect)    → 连片真货仅走 flt_hsv_rescue；不要放宽 flt_aspect
# 筛选·内部(interior)    → inner 全白：红圈破损或模糊填满，缩小 roi / 等清晰帧
# 打分(confidence)       → 看明细贡献列：竖长/偏白低=非感叹号(拒对了)；真货被拒才降 sc_min_conf
# 大 ROI 误判杂红         → 提高 sc_min_conf 到 0.58
#
# ================================================================

_RED_RANGES_DEFAULT = [
    {"lower": [0,   130, 100], "upper": [12,  255, 255]},
    {"lower": [165, 130, 100], "upper": [180, 255, 255]},
]

# 筛选·长宽比闸门(v3)：红块外接框 h/w 允许范围。真红点是圆/菱形，
# 实测紧聚 [0.92, 1.10] 且极端模糊不漂移(抗模糊抗缩放)；两侧各留 ~3 倍余量，
# 容中度连片/边缘裁切，同时杀掉横条(假1≈0.5)与实心柱(§9≈2.1)两类已知杂红。
# 定标依据见 docs/RedDotDetector_打分模型.md §15。
_FLT_ASPECT_DEFAULT = [0.6, 1.6]

# 被长宽比闸拒绝的红块，几何最多留几条进 detail/sampler。仅观测容量，不参与判定。
# 放开到 12（原 3）是为了给"大 ROI 里父块会不会爆"提供分布数据：
# 实测 boss-adb 一帧就有 5 个被拒块，只有 1 个是真红点，旧上限看不出这个比例。
# 总数另记 aspect_rej_n，不受此上限影响。
_ASPECT_REJ_KEEP = 12

# 救援局部重跑窗口在父块外接框四周留的余量(像素)。父块本就完整落在自己的外接框内，
# 理论上 0 亦可；留 2 是防边界效应的保险，实测 0 与 2 结果无差异，成本可忽略。
_RESCUE_PAD = 2

# select_stable_winner 的判定码 → 回传用中文，与金字塔风格一致。
_RESCUE_DECISION_CN = {
    "stable_hit": "稳定命中",
    # stable_hit 的后继态：局部窗口成立、但 active 改判前的全区复跑对不上。
    # 只有 active 走得到（shadow 压根不做全区复核），别把它当成一种救援失败原因。
    "unconfirmed": "复核不一致",
    "no_hit": "未命中",
    "unstable_hit": "命中不稳定",
    "ambiguous_split": "同档多解",
    "ambiguous_stable_hits": "多解歧义",
    "error": "异常",
}
# GUI 日志栏用的一句话失败摘要。完整 hint(含 aspect_rej 明细、调参方向)长达数百字，
# 只进 print 与 detail；日志栏刷全文既挤占用户视野，也容易让人漏看后面的救援结论。
_MISS_BRIEF = {
    "red_mask": "HSV 没框到红色",
    "area": "红块面积不在闸内",
    "aspect": "红块长宽比出圈(疑连片)",
    "interior": "红块内无封闭白芯",
    "confidence": "打分不足",
}

# 每种不成立各自的调参方向；成立时的说明由 _rescue_hint 现算。
_RESCUE_HINT_CN = {
    "no_hit": "三个切法都没从该父块切出合格红点；看 尝试[].卡在 —— "
              "aspect=切得不够狠，interior/red_mask/area=切过头",
    "unconfirmed": "局部结论未能在整幅 ROI 复现，已 fail closed",
    "unstable_hit": "只有单档命中、相邻档不复现；切点疑似落在悬崖边，不予采纳",
    "ambiguous_split": "同一档内该父块切出多个候选，无法认定唯一后代",
    "ambiguous_stable_hits": "出现多个互不相邻的稳定解，按 fail closed 一律拒绝",
    "error": "救援内部异常，已保持 baseline 结果",
}

# ── 打分权重 sc_w_*（v2）─────────────────────────────────────────────
# 设计依据见 docs/RedDotDetector_打分模型.md（真假样本论证）。核心两条：
#   1) 竖长(高瘦) + 偏白(白芯) 抗模糊、是真正的判别金线，并列为主，各 0.45；
#      断层会被 adb 模糊填平/在杂红里造假缝，是反指标，压到最低 0.06，仅作清晰帧奖励。
#   2) 硬不变量：max(单项权重)=0.45 < sc_min_conf=0.55 → 任何单项满分都越不过阈值，
#      命中必须"竖长+偏白"两可靠项同时背书；居中/断层权重小，无法充当假背书。
_SC_W_VERT  = 0.45   # 竖长：高/宽，抗模糊主轴(高瘦感叹号 vs 矮胖杂块)
_SC_W_WHITE = 0.45   # 偏白：白芯相对红环的白度差，抗模糊主轴(白笔画 vs 杂色)
_SC_W_GAP   = 0.06   # 断层：清晰帧小奖励，不可靠(模糊填缝/假缝)，权重压到最低
_SC_W_CENT  = 0.04   # 居中：微调
_SC_MIN_CONF = 0.55  # 默认命中阈值；必须 > 最大单项权重(0.45)，保证两项背书
# 断层"细窄度"：凹陷连续高度 / 内部总高 超过此比例 → 视为"两块不相干"而非缝，细窄度归零
_SC_GAP_MAX_RATIO = 0.40

# 同一检测点(节点名+ROI)两次落盘的最小间隔(秒)，防 next 自循环刷屏；RDD_DUMP_INTERVAL 可调
try:
    _DUMP_MIN_INTERVAL = float(os.environ.get("RDD_DUMP_INTERVAL", "2.0"))
except (TypeError, ValueError):
    _DUMP_MIN_INTERVAL = 2.0   # 环境变量非法时兜底，避免 import 阶段崩溃中断 Agent 启动
_RESOLVED_LOG_DIR = None


def _resolve_log_dir() -> str:
    """
    定位 UI 日志目录(maa.log 所在那层)。判定规则：
      · 环境变量 RDD_DEBUG_DIR 指定 → 直接用；
      · 否则看 interface 本级(Agent CWD)是否有 debug / config：
          有 → 用户侧(interface 在根)，根 = 本级；
          无 → 开发侧(interface 在 assets)，根 = 上一级。
      最终返回 根/debug。结果缓存(CWD 启动后不变)。
    """
    global _RESOLVED_LOG_DIR
    if _RESOLVED_LOG_DIR is not None:
        return _RESOLVED_LOG_DIR

    env = os.environ.get("RDD_DEBUG_DIR")
    if env:
        _RESOLVED_LOG_DIR = env
        return env

    cwd = os.path.abspath(os.getcwd())  # = interface.json 所在目录
    has_marker = (os.path.isdir(os.path.join(cwd, "debug"))
                  or os.path.isdir(os.path.join(cwd, "config")))
    root = cwd if has_marker else os.path.dirname(cwd)
    _RESOLVED_LOG_DIR = os.path.join(root, "debug")
    return _RESOLVED_LOG_DIR


def _save_draw_enabled() -> bool:
    """
    是否执行原生回显(vision 调试图)。优先级：
      · 环境变量 RDD_DRAW(1/true/on) → 强制开/关(=0 是省一次回显识别的总闸)；
      · 否则跟随 MaaFramework 的 save_draw(maa_option.json)。
      · 读不到则默认关。注意：图由框架落盘，若框架 save_draw=false，RDD_DRAW=1
        只会让回显识别执行、图仍不会保存——强制开仅对开关读取失败的场景有意义。
    """
    env = os.environ.get("RDD_DRAW")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(_read_maa_option().get("save_draw", False))


def _read_maa_option() -> dict:
    """读 UI 侧 maa_option.json(<root>/config/)，读不到返回 {}。save_draw 与采样配置共用。"""
    try:
        cfg = os.path.join(os.path.dirname(_resolve_log_dir()), "config", "maa_option.json")
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


# 样本采集器(语料即回归集，详见 rdd_sampler.py 头注)。宿主对 log_dir 的重定向
# agent 侧无 API 可读(MaaGlobalOption 只有 setter)，默认落 <root>/debug/ 同根；
# VSCode 调试等重定向场景用 RDD_SAMPLE_DIR 明示。
_SAMPLER = RddSampler(
    default_dir_fn=lambda: os.path.join(_resolve_log_dir(), "RedDotDetector_samples"),
    option_fn=_read_maa_option,
)


@AgentServer.custom_recognition("RedDotDetector")
class RedDotDetector(CustomRecognition):
    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        """红点感叹号识别器。参数格式与完整示例见模块顶部注释块。"""
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))

            if "preset" in params:
                return self._run_preset(context, argv, params)
            return self._run_standalone(context, argv, params)

        except Exception:
            tb = traceback.format_exc()
            mfaalog.error(f"[RedDotDetector] 执行异常:\n{tb}")
            return CustomRecognition.AnalyzeResult(box=None, detail={
                "result": "error",
                "error": tb.strip().splitlines()[-1],
            })

    # ------------------------------------------------------------------
    # 预设模式
    # ------------------------------------------------------------------

    def _run_preset(self, context: Context,
                    argv: CustomRecognition.AnalyzeArg, params: dict):
        """
        预设模式：将 argv.roi 裁剪后交给预设节点执行，结果坐标加回偏移。
        把"调用者节点名 + 原始 ROI"暂存到 self，供同实例、同步执行的嵌套独立模式读取，
        使失败截图以调用节点名(如 CheckPanel_A)命名。
        """
        preset_node = params["preset"]
        roi = argv.roi
        if roi is not None:
            rx, ry, rw, rh = roi.x, roi.y, roi.w, roi.h
        else:
            rx = ry = rw = rh = 0

        if rw > 0 and rh > 0:
            cropped = argv.image[ry:ry + rh, rx:rx + rw]
        else:
            cropped = argv.image
            rx = ry = 0

        self._caller = (getattr(argv, "node_name", "") or preset_node, (rx, ry, rw, rh))
        try:
            reco = context.run_recognition(preset_node, cropped)
        finally:
            self._caller = None

        # reco is None：识别根本没跑起来(预设节点名写错/被禁用/图像空) —— 配置错误，与漏检区分
        if reco is None:
            mfaalog.error(f"[RedDotDetector] preset 未启动: {preset_node}（节点不存在/被禁用/图像空？）")
            return CustomRecognition.AnalyzeResult(box=None, detail={
                "result": "error", "mode": "preset", "preset": preset_node,
                "roi": [rx, ry, rw, rh],
                "error": f"preset node not started: {preset_node}",
            })

        caller_node = getattr(argv, "node_name", "") or preset_node
        raw = getattr(reco, "raw_detail", None) or {}

        # 预设模式：整屏在本级手里，由本级做原生回显(命中/未命中都画，嵌套层不画)
        echo_hsv, echo_area = self._preset_echo_params(context, preset_node)
        effective_hsv = self._find_nested(raw, "effective_hsv_ranges") or echo_hsv

        if reco.hit:
            bx, by, bw, bh = reco.box
            adjusted = (bx + rx, by + ry, bw, bh)
            self._emit_native_vision(
                context, caller_node, (rx, ry, rw, rh), argv.image,
                effective_hsv, echo_area, result_box=adjusted)
            mfaalog.info(f"[RedDotDetector] [preset:{preset_node}] hit -> {adjusted}")
            return CustomRecognition.AnalyzeResult(
                box=adjusted, detail={"result": "hit", "mode": "preset",
                                      "preset": preset_node,
                                      "preset_detail": raw})

        # 真未命中：阶段原因已由预设节点(独立模式)记进嵌套识别记录；这里附带透传其 raw_detail
        self._emit_native_vision(context, caller_node, (rx, ry, rw, rh),
                                 argv.image, effective_hsv, echo_area)
        mfaalog.warning(f"[RedDotDetector] miss@preset | {argv.node_name} via {preset_node}")
        return CustomRecognition.AnalyzeResult(box=None, detail={
            "result": "miss", "mode": "preset", "preset": preset_node,
            "roi": [rx, ry, rw, rh],
            "preset_detail": raw,
            "hint": "阶段原因见预设节点(独立模式)的 detail；失败截图见 debug/RedDotDetector/ 下以本节点名命名的 rdd_* 文件",
        })

    @staticmethod
    def _find_nested(value, key):
        """在 Maa raw_detail 的不同包装层中取自定义 detail 字段。"""
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = RedDotDetector._find_nested(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = RedDotDetector._find_nested(child, key)
                if found is not None:
                    return found
        return None

    # ------------------------------------------------------------------
    # 独立模式
    # ------------------------------------------------------------------

    # 参数别名：新两级命名(flt_=筛选 / sc_=打分) ↔ 旧名。新名优先、旧名兼容，
    # 既支持已迁移到新名的 pipeline，也不破坏仍用旧名的存量节点。
    _PARAM_ALIAS = {"flt_red_hsv": "hsv_ranges", "flt_area": "red_area",
                    "sc_min_conf": "min_confidence", "sc_gap_ratio": "gap_ratio"}

    def _normalize_params(self, params: dict) -> dict:
        out = dict(params)
        for new, old in self._PARAM_ALIAS.items():
            if new in params:
                out[old] = params[new]
        return out

    def _run_standalone(self, context: Context,
                        argv: CustomRecognition.AnalyzeArg, params: dict):
        """独立模式：HSV 过滤 → blob 面积筛 → 长宽比闸 → 拓扑封闭取内部 → 置信度加权打分。"""
        params = self._normalize_params(params)
        hsv_ranges = params.get("hsv_ranges", _RED_RANGES_DEFAULT)
        area_min, area_max = params.get("red_area", [30, 1200])
        asp_lo, asp_hi = params.get("flt_aspect", _FLT_ASPECT_DEFAULT)
        gap_ratio = params.get("gap_ratio", 0.35)      # 仅用于 detail 的 gap 标注
        min_conf = params.get("min_confidence", _SC_MIN_CONF)
        rescue_cfg, rescue_error = normalize_rescue_config(params.get("flt_hsv_rescue"))
        if rescue_error and "flt_hsv_rescue" in params:
            print(f"[RedDotDetector] HSV 救援配置无效，已关闭: {rescue_error}")

        # 1. 按 roi 裁剪
        roi = argv.roi
        if roi is not None:
            rx, ry, rw, rh = roi.x, roi.y, roi.w, roi.h
        else:
            rx = ry = rw = rh = 0
        if rw > 0 and rh > 0:
            work_bgr = argv.image[ry:ry + rh, rx:rx + rw]
        else:
            work_bgr = argv.image
            rx = ry = 0

        # 预设模式下用调用者(CheckPanel_A)的名字与 ROI 命名截图；否则用自身
        caller = getattr(self, "_caller", None)
        if caller:
            node, key_roi = caller
        else:
            node, key_roi = (getattr(argv, "node_name", "") or ""), (rx, ry, rw, rh)

        # 2. HSV → 红色掩膜
        pil_img = Image.fromarray(work_bgr[..., ::-1])
        hsv_np = np.array(pil_img.convert("HSV"))
        baseline = self._detect_once(
            hsv_np, hsv_ranges, area_min, area_max, asp_lo, asp_hi,
            gap_ratio, min_conf, rx, ry,
        )
        if baseline["hit"]:
            return self._finalize_hit(
                context=context, argv=argv, node=node, key_roi=key_roi,
                caller=caller, work_bgr=work_bgr, roi_tuple=(rx, ry, rw, rh),
                params=params, area_range=(area_min, area_max),
                aspect_range=(asp_lo, asp_hi), gap_ratio=gap_ratio,
                min_conf=min_conf, outcome=baseline,
                candidate=baseline["candidates"][0],
                effective_hsv=hsv_ranges, rescue=None,
            )

        stage, hint = self._diagnose(baseline["stat"], area_min, area_max, min_conf)
        rescue = None
        if stage == "aspect" and rescue_cfg["mode"] != "off":
            try:
                rescue = self._run_hsv_rescue(
                    hsv_np=hsv_np, baseline=baseline, hsv_ranges=hsv_ranges,
                    area_range=(area_min, area_max), aspect_range=(asp_lo, asp_hi),
                    gap_ratio=gap_ratio, min_conf=min_conf, rx=rx, ry=ry,
                    config=rescue_cfg,
                )
            except Exception:
                rescue = {
                    "模式": rescue_cfg["mode"], "结论": _RESCUE_DECISION_CN["error"],
                    "说明": _RESCUE_HINT_CN["error"], "停因": "异常",
                    "_decision": "error", "_winner": None,
                    "错误": traceback.format_exc().strip().splitlines()[-1],
                }
                print(f"[RedDotDetector] HSV 救援异常，保持 baseline miss:\n"
                      f"{traceback.format_exc()}")
            self._log_rescue(node, rescue)
            winner = rescue.get("_winner")
            if (winner is not None and rescue_cfg["mode"] == "active"
                    and rescue.get("_decision") == "stable_hit"):
                confirmed = self._rescue_confirm_full(
                    hsv_np=hsv_np, winner=winner,
                    area_range=(area_min, area_max),
                    aspect_range=(asp_lo, asp_hi), gap_ratio=gap_ratio,
                    min_conf=min_conf, rx=rx, ry=ry)
                if confirmed is None:
                    # 结论必须一起降级：否则下游 _finalize_miss 仍按"稳定命中"渲染，
                    # 日志栏会把 fail closed 说成"mode 不改判"，把人引去查配置。
                    rescue["结论"] = _RESCUE_DECISION_CN["unconfirmed"]
                    rescue["说明"] = _RESCUE_HINT_CN["unconfirmed"]
                    rescue["停因"] = "全区复核不一致"
                    rescue["_decision"] = "unconfirmed"
                    print(f"[RedDotDetector] 救援 {node} | 全区复核不一致，维持 miss")
                else:
                    outcome_full, candidate_full = confirmed
                    return self._finalize_hit(
                        context=context, argv=argv, node=node, key_roi=key_roi,
                        caller=caller, work_bgr=work_bgr,
                        roi_tuple=(rx, ry, rw, rh),
                        params=params, area_range=(area_min, area_max),
                        aspect_range=(asp_lo, asp_hi), gap_ratio=gap_ratio,
                        min_conf=min_conf, outcome=outcome_full,
                        candidate=candidate_full,
                        effective_hsv=winner["profile"],
                        rescue=self._public_rescue(rescue),
                    )

        return self._finalize_miss(
            context=context, argv=argv, node=node, key_roi=key_roi,
            caller=caller, work_bgr=work_bgr, roi_tuple=(rx, ry, rw, rh),
            params=params, hsv_ranges=hsv_ranges,
            area_range=(area_min, area_max),
            aspect_range=(asp_lo, asp_hi), min_conf=min_conf,
            baseline=baseline, stage=stage, hint=hint,
            rescue=self._public_rescue(rescue),
        )

    def _detect_once(self, hsv_np, hsv_ranges, area_min, area_max,
                     asp_lo, asp_hi, gap_ratio, min_conf, rx, ry,
                     red_mask=None, labeled=None, collect_all=False):
        """无日志、采样、回显副作用的单轮检测核心。"""
        red_mask = (_compute_hsv_mask(hsv_np, hsv_ranges)
                    if red_mask is None else red_mask)
        if labeled is None:
            labeled, n_blobs = _label_blobs(red_mask)
        else:
            n_blobs = int(labeled.max()) if labeled.size else 0

        stat = {"red_px": int(red_mask.sum()), "n_blobs": int(n_blobs),
                "area_pass": 0, "aspect_pass": 0, "max_inner_px": 0, "scored": 0}
        best, best_mask, best_box_local = None, None, None
        candidates, eligible_parents = [], []

        for i in range(1, n_blobs + 1):
            blob = (labeled == i)
            area = int(np.sum(blob))
            if not (area_min <= area <= area_max):
                continue
            stat["area_pass"] += 1

            rows = np.where(np.any(blob, axis=1))[0]
            cols = np.where(np.any(blob, axis=0))[0]
            bx0, bx1 = int(cols[0]), int(cols[-1])
            by0, by1 = int(rows[0]), int(rows[-1])
            bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
            aspect = round(bh / max(bw, 1), 2)
            geometry = {"label": i, "x": bx0, "y": by0, "w": bw, "h": bh,
                        "area": area, "aspect": aspect,
                        "fill": round(area / max(bw * bh, 1), 2)}
            if not (asp_lo <= aspect <= asp_hi):
                eligible_parents.append(geometry)
                stat["aspect_rej_n"] = stat.get("aspect_rej_n", 0) + 1
                rej = stat.setdefault("aspect_rej", [])
                if len(rej) < _ASPECT_REJ_KEEP:
                    rej.append(
                        {k: geometry[k] for k in ("w", "h", "area", "aspect", "fill")})
                continue
            stat["aspect_pass"] += 1

            box_red = red_mask[by0:by1 + 1, bx0:bx1 + 1]
            box_hsv = hsv_np[by0:by1 + 1, bx0:bx1 + 1]
            non_red_crop = ~box_red
            labeled_crop, _ = _label_blobs(non_red_crop)
            border_labels = (set(labeled_crop[0, :].tolist())
                             | set(labeled_crop[-1, :].tolist())
                             | set(labeled_crop[:, 0].tolist())
                             | set(labeled_crop[:, -1].tolist()))
            border_labels.discard(0)
            enclosed = non_red_crop & ~np.isin(labeled_crop, list(border_labels))

            enc_px = int(enclosed.sum())
            stat["max_inner_px"] = max(stat["max_inner_px"], enc_px)
            if enc_px == 0:
                continue

            chk = self._exclamation_info(enclosed, gap_ratio)
            conf, parts = self._confidence(box_hsv, box_red, enclosed, chk)
            red_blob = {k: geometry[k] for k in ("w", "h", "area", "aspect", "fill")}
            if best is None or conf > best["conf"]:
                best = {"conf": conf, "parts": parts, "red_blob": red_blob, **chk}
                best_mask = enclosed
                best_box_local = (bx0, by0, bw, bh)

            stat["scored"] += 1
            if conf >= min_conf:
                candidate = {
                    "scan_index": i,
                    "label": i,
                    "box_local": (bx0, by0, bw, bh),
                    "result_box": (bx0 + rx, by0 + ry, bw, bh),
                    "blob_mask": blob,
                    "inner": enclosed,
                    "conf": conf,
                    "parts": parts,
                    "red_blob": red_blob,
                    "chk": chk,
                }
                candidates.append(candidate)
                if not collect_all:
                    stat.update(self._candidate_stat(candidate))
                    return {
                        "hit": True, "candidates": candidates, "stat": stat,
                        "red_mask": red_mask, "labeled": labeled,
                        "eligible_parents": eligible_parents,
                        "best_mask": enclosed, "best_box_local": candidate["box_local"],
                    }

        if best is not None:
            stat["conf"] = best["conf"]
            stat["parts"] = best["parts"]
            stat["red_blob"] = best["red_blob"]
            stat["proj"] = best["proj"]
            stat["gap"] = {"row": best["gap_row"], "val": best["gap_val"],
                           "peak": best["peak"], "ratio": best["ratio"],
                           "above_nz": best["above_nz"],
                           "has_below": best["has_below"]}
            stat["best_box"] = list(best_box_local) if best_box_local else None

        return {
            "hit": bool(candidates), "candidates": candidates, "stat": stat,
            "red_mask": red_mask, "labeled": labeled,
            "eligible_parents": eligible_parents,
            "best_mask": best_mask, "best_box_local": best_box_local,
        }

    @staticmethod
    def _candidate_stat(candidate):
        chk = candidate["chk"]
        return {
            "conf": candidate["conf"],
            "parts": candidate["parts"],
            "red_blob": candidate["red_blob"],
            "proj": chk.get("proj"),
            "gap": {"row": chk.get("gap_row"), "val": chk.get("gap_val"),
                    "peak": chk.get("peak"), "ratio": chk.get("ratio"),
                    "above_nz": chk.get("above_nz"),
                    "has_below": chk.get("has_below")},
        }

    def _rescue_try(self, *, hsv_np, baseline, geo, delta_s, delta_v,
                    hsv_ranges, area_range, aspect_range, gap_ratio,
                    min_conf, rx, ry):
        """在**父块外接框**内跑一次完整检测链，返回 (候选, 卡在, 红像素数)。

        只在局部重跑而非整个 ROI：救援的判定被 lineage 限定在这一个父块内，
        框选区其余像素怎么变都与结论无关。实测 23 处对比与全区重跑逐位一致，
        平均提速 5.8 倍，且成本随父块（受面积闸约束 ≤ area_max）而非 ROI 增长。
        """
        profile = strict_profile(hsv_ranges, delta_s, delta_v)
        if profile is None:
            return None, "参数", 0

        h, w = hsv_np.shape[:2]
        x0 = max(0, int(geo["x"]) - _RESCUE_PAD)
        y0 = max(0, int(geo["y"]) - _RESCUE_PAD)
        x1 = min(w, int(geo["x"]) + int(geo["w"]) + _RESCUE_PAD)
        y1 = min(h, int(geo["y"]) + int(geo["h"]) + _RESCUE_PAD)
        if x1 <= x0 or y1 <= y0:
            return None, "窗口", 0

        sub_hsv = hsv_np[y0:y1, x0:x1]
        sub_base_mask = baseline["red_mask"][y0:y1, x0:x1]
        sub_base_lab = baseline["labeled"][y0:y1, x0:x1]

        out = self._detect_once(
            sub_hsv, profile, area_range[0], area_range[1],
            aspect_range[0], aspect_range[1], gap_ratio, min_conf,
            rx + x0, ry + y0, collect_all=True,
        )
        red_px = int(out["stat"]["red_px"])
        # 严格子集：只许删红像素，不许凭空添红。
        if not is_strict_mask(out["red_mask"], sub_base_mask):
            return None, "子集", red_px

        for cand in out["candidates"]:
            if lineage_parent(cand["blob_mask"], sub_base_lab,
                              [geo["label"]]) != geo["label"]:
                continue
            bx, by, bw, bh = cand["box_local"]
            cand["box_local"] = (bx + x0, by + y0, bw, bh)   # 换回 ROI 坐标系
            cand["profile"] = profile
            return cand, None, red_px

        # 没有血统合格的候选：借 baseline 同一套诊断词表说明卡在哪步，
        # 好让 aspect(切得不够狠) 与 interior/red_mask(切过头) 一眼可分。
        if out["candidates"]:
            return None, "血统", red_px
        stage, _ = self._diagnose(out["stat"], area_range[0], area_range[1], min_conf)
        return None, stage, red_px

    def _run_hsv_rescue(self, *, hsv_np, baseline, hsv_ranges, area_range,
                        aspect_range, gap_ratio, min_conf, rx, ry, config):
        """严格 HSV 救援：切点由父块自身分布算出，局部重跑，跨档复现验收。

        流程（每个被长宽比闸拒的父块，按"像不像红点"排序后依次处理）：
          1. 取该父块像素，两个通道各做一次直方图 → Otsu 切点（微秒级）
          2. 三个切法(只切亮度/只切饱和/双切)各跑一次**主档**
          3. 命中位置一致 → 取增量最小的切法；位置分散 → 判方向歧义，拒绝
          4. 对选中切法补跑上下**陪跑档**，交 select_stable_winner 判跨档稳定
        任一环节失败即换下一个父块；预算耗尽维持 baseline miss。
        """
        started = time.perf_counter()
        budget_ms = config["time_budget_ms"]

        def timed_out():
            return (time.perf_counter() - started) * 1000 >= budget_ms

        def elapsed():
            return round((time.perf_counter() - started) * 1000, 2)

        parents = sort_parents(baseline["eligible_parents"])
        parent_rows, attempts = [], []
        full_runs = 0
        tried_parents = 0
        winner = decision = support = None
        cutpoints = win_direction = win_main = None
        stop_reason = "父块用尽"

        for geo in parents:
            row = {"序": len(parent_rows) + 1,
                   "几何": f"{geo['w']}x{geo['h']}", "面积": geo["area"],
                   "长宽比": geo["aspect"], "填充": geo["fill"], "试了": False}
            parent_rows.append(row)
            if winner is not None:
                continue
            if tried_parents >= config["max_parents"]:
                row["跳过"] = "父块预算"
                continue
            if timed_out():
                row["跳过"] = "超时"
                stop_reason = "超时"
                continue

            pixels = hsv_np[baseline["labeled"] == geo["label"]]
            cuts = channel_cutpoints(pixels, hsv_ranges, config)
            if cuts is None:
                row["跳过"] = "无法定点"
                continue
            tried_parents += 1
            row["试了"] = True

            # —— 第 2 步：三个切法各跑一次主档 ——
            probes, seen_deltas = {}, set()
            for direction in DIRECTIONS:
                if timed_out():
                    stop_reason = "超时"
                    break
                if full_runs >= config["max_full_runs"]:
                    stop_reason = "重跑预算"
                    break
                pair = direction_deltas(cuts, direction)
                if pair is None or pair in seen_deltas:
                    continue          # 例如某通道增量为 0 时，双切与单切等价
                seen_deltas.add(pair)
                cand, blocked, red_px = self._rescue_try(
                    hsv_np=hsv_np, baseline=baseline, geo=geo,
                    delta_s=pair[0], delta_v=pair[1], hsv_ranges=hsv_ranges,
                    area_range=area_range, aspect_range=aspect_range,
                    gap_ratio=gap_ratio, min_conf=min_conf, rx=rx, ry=ry)
                full_runs += 1
                attempts.append(self._rescue_attempt(
                    row["序"], direction, pair, red_px, blocked, cand))
                if cand is not None:
                    probes[direction] = (pair, cand)

            if not probes:
                continue
            # —— 第 3 步：不同切法必须指向同一处，否则是真歧义 ——
            if not boxes_agree([c["box_local"] for _, c in probes.values()]):
                row["跳过"] = "方向歧义"
                stop_reason = "方向歧义"
                continue

            win_direction = min(probes, key=lambda d: sum(probes[d][0]))
            (ds, dv), main_cand = probes[win_direction]

            # —— 第 4 步：补跑陪跑档，交既有的跨档稳定判定 ——
            records = []
            for state in neighbor_states(win_direction, ds, dv, config):
                is_main = (state["delta_s"], state["delta_v"]) == (ds, dv)
                cand = main_cand if is_main else None
                if cand is None:
                    if timed_out():
                        stop_reason = "超时"
                        break
                    if full_runs >= config["max_full_runs"]:
                        stop_reason = "重跑预算"
                        break
                    cand, blocked, red_px = self._rescue_try(
                        hsv_np=hsv_np, baseline=baseline, geo=geo,
                        delta_s=state["delta_s"], delta_v=state["delta_v"],
                        hsv_ranges=hsv_ranges, area_range=area_range,
                        aspect_range=aspect_range, gap_ratio=gap_ratio,
                        min_conf=min_conf, rx=rx, ry=ry)
                    full_runs += 1
                    attempts.append(self._rescue_attempt(
                        row["序"], win_direction + "·陪跑",
                        (state["delta_s"], state["delta_v"]),
                        red_px, blocked, cand))
                if cand is None:
                    continue
                records.append({
                    "state": state, "parent_id": geo["label"],
                    "box_local": cand["box_local"],
                    "scan_index": cand["scan_index"],
                    "candidate": cand, "profile": cand["profile"],
                })

            cand_winner, cand_decision, cand_support = select_stable_winner(
                records, config["min_stable_states"])
            decision, support = cand_decision, cand_support
            cutpoints = cuts
            if cand_decision == "stable_hit":
                winner = cand_winner
                win_main = (ds, dv)
                stop_reason = "稳定命中"
            else:
                row["跳过"] = cand_decision

        if decision is None:
            decision = "no_hit"
        public = {
            "模式": config["mode"],
            "结论": _RESCUE_DECISION_CN.get(decision, decision),
            "说明": self._rescue_hint(decision, win_direction, cutpoints,
                                      winner, support),
            "耗时ms": elapsed(),
            "切点": cutpoints,
            "尝试": attempts,
            "父块": {"总数": len(parents), "已试": tried_parents,
                     "跳过": len(parents) - tried_parents, "明细": parent_rows},
            "预算": {"重跑": full_runs, "上限": config["max_full_runs"],
                     "耗时ms": elapsed(), "上限ms": budget_ms},
            "停因": stop_reason,
            "_decision": decision,
            "_winner": winner,
        }
        if winner is not None:
            public["胜出"] = {
                "方向": win_direction,
                # 主档 = 切点直接算出的那一档；采纳档 = 复现组里最保守(增量最小)的一档。
                # 两者可以不同，这不是矛盾：切点定位置，复现组决定最终取哪一档。
                "主档增量": list(win_main) if win_main else None,
                "采纳增量": [winner["state"]["delta_s"],
                             winner["state"]["delta_v"]],
                "框": list(winner["box_local"]),
                "分": winner["candidate"]["conf"],
                "复现": len(support[0]["states"]) if support else 0,
            }
        return public

    @staticmethod
    def _rescue_attempt(parent_no, direction, pair, red_px, blocked, cand):
        """一条尝试记录。失败档不带 框/分（恒为空），省体积也少一层噪声。"""
        row = {"父块": parent_no, "方向": direction,
               "增量": [int(pair[0]), int(pair[1])], "红像素": int(red_px),
               "卡在": blocked}
        if cand is not None:
            row["框"] = list(cand["box_local"])
            row["分"] = cand["conf"]
        return row

    @staticmethod
    def _rescue_hint(decision, direction, cutpoints, winner, support):
        """一句话说清这次救援凭什么成立 / 因何不成立，措辞与调参方向对齐。"""
        if decision == "stable_hit" and winner is not None:
            ds, dv = winner["state"]["delta_s"], winner["state"]["delta_v"]
            base = []
            if cutpoints:
                if dv > 0:
                    base.append(f"亮度切点{cutpoints['亮度']['Otsu']}"
                                f"(基线{cutpoints['亮度']['基线']})")
                if ds > 0:
                    base.append(f"饱和切点{cutpoints['饱和']['Otsu']}"
                                f"(基线{cutpoints['饱和']['基线']})")
            times = len(support[0]["states"]) if support else 0
            return (f"切法{direction}；{'；'.join(base)}；"
                    f"相邻{times}档复现同框，采纳最保守档 ΔS{ds}/ΔV{dv}；血统同源")
        return _RESCUE_HINT_CN.get(decision, f"未成立({decision})")

    @staticmethod
    def _log_rescue(node, rescue):
        """救援只走 print 单行摘要：ΔS/ΔV、lineage 等细节留在 detail.救援 里，
        由框架随 reco_details 落进 maa 核心日志，不占 GUI 日志栏（见观测契约）。"""
        if not rescue:
            return
        budget = rescue.get("预算") or {}
        head = f"[RedDotDetector] 救援 {node} | {rescue.get('结论')}"
        win = rescue.get("胜出")
        if win:
            tail = (f"{win['方向']}ΔS{win['采纳增量'][0]}/ΔV{win['采纳增量'][1]} "
                    f"复现{win['复现']}档 → box={win['框']} 分{win['分']}")
        else:
            blocked = [a.get("卡在") for a in (rescue.get("尝试") or [])
                       if a.get("卡在")]
            uniq = sorted(set(blocked))
            tail = f"停因={rescue.get('停因')}" + (f" 卡在={','.join(uniq)}" if uniq else "")
        print(f"{head} | {tail} | {budget.get('重跑', 0)}重跑 "
              f"{rescue.get('耗时ms', 0)}ms")

    def _rescue_confirm_full(self, *, hsv_np, winner, area_range, aspect_range,
                             gap_ratio, min_conf, rx, ry):
        """active 改判前，用胜出参数在**整个 ROI** 复跑一次。

        两个作用：① 取回整幅红掩膜，供 sampler / 原生回显落盘（局部窗口的掩膜尺寸对不上）；
        ② 作为局部结论的最后一道交叉校验 —— 全区跑不出同一个框就 fail closed。
        只在 active 且已有稳定解时执行，属低频路径。
        """
        outcome = self._detect_once(
            hsv_np, winner["profile"], area_range[0], area_range[1],
            aspect_range[0], aspect_range[1], gap_ratio, min_conf, rx, ry,
            collect_all=True,
        )
        target = tuple(winner["box_local"])
        for cand in outcome["candidates"]:
            if tuple(cand["box_local"]) == target:
                return outcome, cand
        return None

    @staticmethod
    def _public_rescue(rescue):
        if not rescue:
            return None
        return {k: v for k, v in rescue.items() if not k.startswith("_")}

    def _finalize_hit(self, *, context, argv, node, key_roi, caller,
                      work_bgr, roi_tuple, params, area_range, aspect_range,
                      gap_ratio, min_conf, outcome, candidate,
                      effective_hsv, rescue):
        area_min, area_max = area_range
        asp_lo, asp_hi = aspect_range
        result_box = candidate["result_box"]
        conf, parts = candidate["conf"], candidate["parts"]
        red_blob, chk = candidate["red_blob"], candidate["chk"]
        stat = {**outcome["stat"], **self._candidate_stat(candidate)}
        effective_params = {**params, "hsv_ranges": effective_hsv,
                            "red_area": [area_min, area_max],
                            "flt_aspect": [asp_lo, asp_hi],
                            "gap_ratio": gap_ratio,
                            "min_confidence": min_conf}
        dbg_text = self._compose_dbg(
            effective_params, stat, f"result: HIT box={list(result_box)}", min_conf)
        mfaalog.info(f"[RedDotDetector] hit | box={result_box} conf={conf} {parts}")
        print(f"[RedDotDetector] {node}\n{dbg_text}")

        configured_hsv = params.get("hsv_ranges", _RED_RANGES_DEFAULT)
        meta = {
            "mode": "preset" if caller else "standalone",
            "box": list(result_box), "conf": conf, "parts": parts,
            "red_blob": red_blob, "proj": chk.get("proj"),
            "params": {"hsv_ranges": configured_hsv,
                       "configured_hsv_ranges": configured_hsv,
                       "effective_hsv_ranges": effective_hsv,
                       "red_area": [area_min, area_max],
                       "flt_aspect": [asp_lo, asp_hi],
                       "gap_ratio": gap_ratio,
                       "min_conf": min_conf,
                       "flt_hsv_rescue": params.get("flt_hsv_rescue")},
        }
        if rescue:
            meta["救援"] = rescue
        _SAMPLER.record(
            node=node, roi=key_roi, result="hit",
            images={"roi_crop": work_bgr, "red_mask": outcome["red_mask"],
                    "inner": candidate["inner"]},
            meta=meta,
        )
        if caller is None:
            self._emit_native_vision(
                context, node, roi_tuple, argv.image, effective_hsv, area_min,
                result_box=result_box)

        detail = {
            "result": "hit", "阶段": "打分",
            "mode": "preset" if caller else "standalone",
            "打分": self._score_breakdown(parts, conf, min_conf),
            "red_area": red_blob["area"], "red_blob": red_blob,
            "box": list(result_box), "conf": conf, "parts": parts,
            "effective_hsv_ranges": effective_hsv, "dbg": dbg_text,
        }
        if rescue:
            detail["救援"] = rescue
        return CustomRecognition.AnalyzeResult(box=result_box, detail=detail)

    def _finalize_miss(self, *, context, argv, node, key_roi, caller,
                       work_bgr, roi_tuple, params, hsv_ranges, area_range,
                       aspect_range, min_conf, baseline, stage, hint, rescue):
        area_min, area_max = area_range
        asp_lo, asp_hi = aspect_range
        stat = baseline["stat"]
        effective_params = {**params, "hsv_ranges": hsv_ranges,
                            "red_area": [area_min, area_max],
                            "flt_aspect": [asp_lo, asp_hi],
                            "min_confidence": min_conf}
        dbg_text = self._compose_dbg(
            effective_params, stat, f"result: MISS ({stage}) {hint}", min_conf)
        dump_info = self._dump_failure(
            node, key_roi, work_bgr, baseline["red_mask"], baseline["best_mask"])
        miss_imgs = {"roi_crop": work_bgr, "red_mask": baseline["red_mask"]}
        if baseline["best_mask"] is not None:
            miss_imgs["inner"] = baseline["best_mask"]
        best_box_local = baseline["best_box_local"]
        meta = {
            "mode": "preset" if caller else "standalone",
            "box": ([best_box_local[0] + roi_tuple[0],
                     best_box_local[1] + roi_tuple[1],
                     best_box_local[2], best_box_local[3]]
                    if best_box_local else None),
            "conf": stat.get("conf"), "parts": stat.get("parts"),
            "red_blob": stat.get("red_blob"), "proj": stat.get("proj"),
            "gap": stat.get("gap"), "aspect_rej": stat.get("aspect_rej"),
            "aspect_rej_n": stat.get("aspect_rej_n"),
            "filter_stat": {
                "red_px": stat["red_px"], "n_blobs": stat["n_blobs"],
                "area_pass": stat["area_pass"],
                "aspect_pass": stat.get("aspect_pass"),
                "aspect_rej_n": stat.get("aspect_rej_n", 0),
                "max_inner_px": stat["max_inner_px"], "scored": stat["scored"],
            },
            "params": {"hsv_ranges": hsv_ranges,
                       "configured_hsv_ranges": hsv_ranges,
                       "effective_hsv_ranges": hsv_ranges,
                       "red_area": [area_min, area_max],
                       "flt_aspect": [asp_lo, asp_hi],
                       "gap_ratio": params.get("gap_ratio", 0.35),
                       "min_conf": min_conf,
                       "flt_hsv_rescue": params.get("flt_hsv_rescue")},
            "failure_dump": dump_info,
        }
        if rescue:
            meta["救援"] = rescue
        _SAMPLER.record(
            node=node, roi=key_roi, result="miss", stage=stage,
            images=miss_imgs, meta=meta,
        )
        if caller is None:
            self._emit_native_vision(
                context, node, roi_tuple, argv.image, hsv_ranges, area_min)
        extra = {"failure_dump": dump_info}
        if rescue:
            extra["救援"] = rescue
        return self._miss(
            "preset" if caller else "standalone", stage, hint, stat, effective_params,
            dbg_text, min_conf, extra=extra)

    # ------------------------------------------------------------------
    # 感叹号结构检测：返回投影 + 断层诊断信息
    # ------------------------------------------------------------------

    def _exclamation_info(self, region: np.ndarray, gap_ratio: float) -> dict:
        """
        从内部封闭区的垂直投影提取断层信息（供 f_gap 与诊断使用）。
        返回 {pass, proj, gap_row, gap_val, peak, ratio, above_nz, has_below}。
        pass 仅作"是否成清晰双段"的标注，命中与否由 _confidence 决定。
        """
        proj_arr = np.sum(region, axis=1).astype(np.float32)
        info = {"pass": False, "proj": proj_arr.astype(int).tolist(),
                "gap_row": None, "gap_val": None,
                "peak": int(proj_arr.max()) if proj_arr.size else 0,
                "ratio": None, "above_nz": 0, "has_below": False,
                "gap_run": 0, "height": 0}   # 凹陷连续高度 / 内部总高（三段诚实断层用）

        if int(proj_arr.sum()) < 3:
            return info
        nz = np.where(proj_arr > 0)[0]
        if len(nz) < 2:
            return info

        first_nz, last_nz = int(nz[0]), int(nz[-1])
        trimmed = proj_arr[first_nz:last_nz + 1]  # 去掉包围框上下空白行
        if len(trimmed) < 3:                      # 区段太短：长宽比兜底
            ph, pw = region.shape
            info["pass"] = ph > pw * 1.3
            return info

        gap_abs = first_nz + int(np.argmin(trimmed[1:-1])) + 1   # 断层行(原始下标)
        peak = float(proj_arr.max())
        if peak == 0:
            return info

        ratio = float(proj_arr[gap_abs] / peak)
        above_nz = int(np.sum(proj_arr[first_nz:gap_abs] > 0))           # 竖线段非零行数
        has_below = bool(np.any(proj_arr[gap_abs + 1:last_nz + 1] > 0))  # 圆点段是否有像素

        # 凹陷连续高度 gap_run：从谷底向上下扩张，统计"低于浅阈值"的连续行数。
        # 真感叹号的缝只占一两行；假货是大段空白(两块不相干) → gap_run 占比大。
        height = last_nz - first_nz + 1
        gap_low = max(0.0, 0.25 * peak)
        lo = hi = gap_abs
        while lo - 1 >= first_nz and proj_arr[lo - 1] <= gap_low:
            lo -= 1
        while hi + 1 <= last_nz and proj_arr[hi + 1] <= gap_low:
            hi += 1
        gap_run = hi - lo + 1

        info.update({"gap_row": gap_abs + 1, "gap_val": int(proj_arr[gap_abs]),
                     "peak": int(peak), "ratio": round(ratio, 3),
                     "above_nz": above_nz, "has_below": has_below,
                     "gap_run": int(gap_run), "height": int(height),
                     "pass": (ratio <= gap_ratio) and above_nz >= 2 and has_below})
        return info

    # ------------------------------------------------------------------
    # 置信度加权：gap(连续) + 纵横比 + 偏白对比 + 居中
    # ------------------------------------------------------------------

    @staticmethod
    def _whiteness(V: np.ndarray, S: np.ndarray) -> np.ndarray:
        """偏白程度 ∈[0,1]：亮(V↑)且低饱和(S↓)。"""
        return (V.astype(np.float32) / 255.0) * (1.0 - S.astype(np.float32) / 255.0)

    def _confidence(self, box_hsv: np.ndarray, box_red: np.ndarray,
                    enclosed: np.ndarray, chk: dict):
        """对一个候选打 0~1 置信分，返回 (conf, parts)。各分项均为归一化抗模糊量。"""
        ys, xs = np.where(enclosed)
        if len(ys) < 2:
            return 0.0, {"by": "no_inner"}

        # f_gap：三段诚实断层 = 深度 × 细窄度（仅清晰帧小奖励，权重最低）。
        #   感叹号是 上段/凹陷/下段 三段；旧版只看"有没有一根空行"，把凹陷当点不当段，
        #   于是杂红里的大段空白也被当成满分缝。现在把凹陷高度纳入：
        #     深度   = 1 − 谷底像素/最高行像素     （谷有多空）
        #     细窄度 = 1 − (凹陷高度/总高)/上限比例 （缝越宽越不像真缝，越接近"两块不相干"）
        #   两侧仍需各有料(上段≥1行 且 下段有像素)，否则不成断层 → 0。
        peak = chk.get("peak") or 0
        gap_run = chk.get("gap_run", 0)
        height = chk.get("height", 0)
        if peak and chk.get("has_below") and chk.get("above_nz", 0) >= 1 and height > 0:
            depth = float(np.clip(1.0 - chk["gap_val"] / peak, 0.0, 1.0))
            thinness = float(np.clip(1.0 - (gap_run / height) / _SC_GAP_MAX_RATIO, 0.0, 1.0))
            f_gap = depth * thinness
        else:
            f_gap = 0.0

        # f_vert：高宽比 h/w。高瘦感叹号 h/w≥2 → 1；矮胖杂块 h/w≈1 → 0。抗模糊主轴之一。
        h = int(ys.max() - ys.min() + 1)
        w = int(xs.max() - xs.min() + 1)
        f_vert = float(np.clip(h / max(w, 1) - 1.0, 0.0, 1.0))

        # f_white：偏白"对比"。内部白度中位数 − 红环白度中位数；模糊压暗时平滑衰减，不掉崖。
        #   抗模糊主轴之一：白笔画恒比红环白，杂块内部不白 → 这项是真假最干净的金线。
        V, S = box_hsv[..., 2], box_hsv[..., 1]
        w_in = float(np.median(self._whiteness(V[enclosed], S[enclosed])))
        w_rng = float(np.median(self._whiteness(V[box_red], S[box_red]))) if box_red.any() else 0.0
        f_white = float(np.clip(w_in - w_rng, 0.0, 1.0))

        # f_cent：内部水平中心 vs 包围框中心（弱分离，微调）
        cx_e = (int(xs.min()) + int(xs.max())) / 2.0
        cx_c = (enclosed.shape[1] - 1) / 2.0
        f_cent = float(np.clip(1.0 - 2.0 * abs(cx_e - cx_c) / max(enclosed.shape[1], 1), 0.0, 1.0))

        conf = (_SC_W_VERT * f_vert + _SC_W_WHITE * f_white
                + _SC_W_GAP * f_gap + _SC_W_CENT * f_cent)
        parts = {"gap": round(f_gap, 2), "vert": round(f_vert, 2),
                 "white": round(f_white, 2), "cent": round(f_cent, 2)}
        return round(float(conf), 3), parts

    # ------------------------------------------------------------------
    # 失败诊断 / 统一出口
    # ------------------------------------------------------------------

    # 打分项：英文代号 → 中文名 → 权重（金字塔回调与调试文本共用）
    _SCORE_META = (("vert", "竖长", _SC_W_VERT), ("white", "偏白", _SC_W_WHITE),
                   ("gap", "断层", _SC_W_GAP), ("cent", "居中", _SC_W_CENT))

    def _score_breakdown(self, parts: dict, conf, threshold) -> dict:
        """金字塔第三层：每项 值/权重/贡献(=值×权重)，一眼看出谁把分顶上去。"""
        parts = parts or {}
        items = [{"项": cn, "值": round(float(parts.get(en, 0.0)), 3),
                  "权重": w, "贡献": round(float(parts.get(en, 0.0)) * w, 3)}
                 for en, cn, w in self._SCORE_META]
        return {"总分": round(float(conf), 3) if conf is not None else None,
                "阈值": round(float(threshold), 3) if threshold is not None else None,
                "通过": bool(conf is not None and threshold is not None and conf >= threshold),
                "明细": items}

    def _diagnose(self, stat: dict, area_min, area_max, min_conf):
        """根据累加器判定卡在哪个阶段，给出修正方向。"""
        if stat["red_px"] == 0:
            return "red_mask", "HSV 未覆盖到任何红色，降低 S/V 下限，或确认 roi 框住了红点"
        if stat["n_blobs"] == 0:
            return "red_mask", f"有红像素但未成连通域(red_px={stat['red_px']})，检查红色是否破碎"
        if stat["area_pass"] == 0:
            return "area", f"红色面积都不在 [{area_min},{area_max}]，调 flt_area（多半是 min 太大）"
        if stat.get("aspect_pass", 0) == 0:
            rej = stat.get("aspect_rej") or []
            total = stat.get("aspect_rej_n", len(rej))
            return "aspect", (f"红块 h/w 都不在 flt_aspect 内(被拒 {total} 块，"
                              f"前 3: {rej[:3]})；横条/竖柱杂红属正常拒；若是连片真货被误杀，"
                              "由 flt_hsv_rescue 严格 HSV 搜索拆开连片；不要放宽 flt_aspect")
        if stat["max_inner_px"] == 0:
            return "interior", "红块内无封闭非红区(无感叹号轮廓)：roi 偏移 / 红圈破损 / 被模糊填满"
        return "confidence", (f"最高置信 {stat.get('conf')} < sc_min_conf({min_conf})；"
                              f"分项 {stat.get('parts')}；降低 sc_min_conf 提召回，"
                              f"或检查偏白/竖向是否被模糊吃掉")

    def _miss(self, mode: str, stage: str, hint: str, stat: dict, params: dict,
              dbg_text: str = None, min_conf=None, extra: dict = None):
        """
        统一失败出口（金字塔回调）：
          第1层 result=miss；第2层 阶段=筛选/打分；
          阶段=筛选 → 给"卡在哪步 + 筛选计数"；阶段=打分 → 给"总分/阈值 + 每项值/权重/贡献"。
        stat/conf/parts 等保留，供 preset 上层画图与向后兼容。
        """
        if stage == "confidence":      # 走到了打分、但分不够
            detail = {
                "result": "miss", "阶段": "打分",
                "打分": self._score_breakdown(stat.get("parts"), stat.get("conf"), min_conf),
                "说明": hint, "mode": mode, "stat": stat, "dbg": dbg_text,
            }
        else:                           # 卡在筛选某步(红掩膜/面积/内部)
            detail = {
                "result": "miss", "阶段": "筛选", "卡在": stage, "说明": hint, "mode": mode,
                "数据": {"红像素": stat.get("red_px"), "连块数": stat.get("n_blobs"),
                        "过面积": stat.get("area_pass"), "过长宽比": stat.get("aspect_pass"),
                        "内部像素": stat.get("max_inner_px"), "打分数": stat.get("scored")},
                "stat": stat, "dbg": dbg_text,
            }
        if extra:
            detail.update(extra)
        # 救援找到了解却仍 miss，有两种原因，日志栏必须分清楚 —— 否则用户看到的
        # 只是一条 miss，会误判成救援没工作，或把 fail closed 错当成 mode 配置问题：
        #   · 稳定命中   → shadow 只记录不改判，改 mode 即可生效
        #   · 复核不一致 → active 已经想改判，是全区复跑对不上才主动放弃，与 mode 无关
        rescue = (extra or {}).get("救援") or {}
        tail = ""
        if rescue.get("结论") == "稳定命中":
            win = rescue.get("胜出") or {}
            tail = (f"｜严格HSV救援已找到稳定解 box={win.get('框')} 分{win.get('分')}"
                    f"，当前 mode={rescue.get('模式')} 不改判")
        elif rescue.get("结论") == "复核不一致":
            win = rescue.get("胜出") or {}
            tail = (f"｜严格HSV救援局部解 box={win.get('框')} 未能在整幅 ROI 复现"
                    f"，已 fail closed 维持 miss")
        elif rescue.get("attempted") or rescue.get("尝试"):
            tail = f"｜救援未成立({rescue.get('结论')})"
        mfaalog.warning(f"[RedDotDetector] miss@{stage} | "
                        f"{_MISS_BRIEF.get(stage, stage)}{tail}")
        print(f"[RedDotDetector] miss\n{dbg_text}" if dbg_text else f"[RedDotDetector] miss stat={stat}")
        return CustomRecognition.AnalyzeResult(box=None, detail=detail)

    # ------------------------------------------------------------------
    # 调试图：常驻、固定命名(覆盖)、时间节流；路径走 print
    # ------------------------------------------------------------------

    def _bool_to_bgr(self, mask: np.ndarray) -> np.ndarray:
        """bool mask → 白底黑形状 BGR 图。"""
        out = np.full((*mask.shape, 3), 255, dtype=np.uint8)
        out[mask] = [0, 0, 0]
        return out

    def _img_key(self, node_name: str, roi_tuple) -> str:
        """文件名 key = 节点名 + ROI。同检测点重复失败 → 同名覆盖；不同面板 ROI 不同 → 不互相覆盖。"""
        raw = f"{node_name or 'node'}_{roi_tuple[0]}-{roi_tuple[1]}-{roi_tuple[2]}-{roi_tuple[3]}"
        return re.sub(r'[^A-Za-z0-9_.\-]', '_', raw)

    def _save_debug_img(self, bgr_img: np.ndarray, key: str, tag: str):
        try:
            debug_dir = os.path.join(_resolve_log_dir(), "RedDotDetector")
            os.makedirs(debug_dir, exist_ok=True)
            path = os.path.abspath(os.path.join(debug_dir, f"rdd_{key}_{tag}.png"))
            Image.fromarray(bgr_img[..., ::-1]).save(path)  # 同名覆盖
            return path
        except Exception as e:
            print(f"[RedDotDetector] 调试图保存失败({tag}): {e}")
            return None

    # ------------------------------------------------------------------
    # 原生回显：借内置 ColorMatch 生成框架原生 vision 图（带 reco_id）
    # ------------------------------------------------------------------

    def _emit_native_vision(self, context: Context, node: str, roi_tuple,
                            image: np.ndarray, hsv_ranges: list, area_min,
                            result_box=None) -> None:
        """
        在整屏原图上跑一次内置 ColorMatch(method 40，同 hsv_ranges)，由框架原生画图：
        绿框=ROI，红字 R:[box]=红色 blob 包围框(与本识别器返回的 box 是同一个红块)。
        原生图带 reco_id 由框架落盘到 <log_dir>/vision，MaaLogAnalyzer / MaaLogs 直接显示。
        受 save_draw 门控；回显命中与否不影响识别结果，仅为可视化。
        """
        if not _save_draw_enabled() or image is None:
            return
        try:
            lowers = [r.get("lower") or r.get("lower_hsv") for r in hsv_ranges]
            uppers = [r.get("upper") or r.get("upper_hsv") for r in hsv_ranges]
            if not lowers or any(v is None for v in lowers + uppers):
                return
            echo = f"{node or 'RedDotDetector'}__RDDraw"
            context.run_recognition(echo, image, pipeline_override={
                echo: {
                    "recognition": "ColorMatch",
                    # 命中时限制到实际返回框，避免大 ROI 内 ColorMatch 画出另一个更大红块。
                    "roi": list(result_box if result_box is not None else roi_tuple),
                    "method": 40,   # HSV，与 hsv_ranges 同为 OpenCV 坐标系(H 0-180)
                    "lower": lowers,
                    "upper": uppers,
                    "connected": True,
                    "count": max(1, int(area_min)),
                },
            })
        except Exception as e:
            print(f"[RedDotDetector] 原生回显失败: {e}")

    def _preset_echo_params(self, context: Context, preset_node: str):
        """从预设节点定义里取回显所需的 HSV 范围 / 面积下限；取不到用默认。

        节点里写的是新名(flt_red_hsv / flt_area)，_normalize_params 翻成内部旧名后再取。
        """
        try:
            data = context.get_node_data(preset_node) or {}
            p = self._normalize_params(data.get("custom_recognition_param") or {})
            return (p.get("hsv_ranges", _RED_RANGES_DEFAULT),
                    p.get("red_area", [30, 1200])[0])
        except Exception:
            return _RED_RANGES_DEFAULT, 30

    def _compose_dbg(self, params: dict, stat: dict, result_line: str, threshold=None) -> str:
        """把所有调试量拼成一个字符串：print 与 detail 共用，加减参数只改这里。"""
        hr = params.get("hsv_ranges", _RED_RANGES_DEFAULT)
        lines = [
            result_line,
            f"params: flt_area={params.get('red_area', [30, 1200])} "
            f"flt_aspect={params.get('flt_aspect', _FLT_ASPECT_DEFAULT)} "
            f"sc_min_conf={params.get('min_confidence', _SC_MIN_CONF)} hsv_groups={len(hr)}",
            f"stat: red_px={stat.get('red_px')} n_blobs={stat.get('n_blobs')} "
            f"area_pass={stat.get('area_pass')} aspect_pass={stat.get('aspect_pass')} "
            f"inner_px={stat.get('max_inner_px')} scored={stat.get('scored')}",
        ]
        if stat.get("aspect_rej"):
            rej = stat["aspect_rej"]
            total = stat.get("aspect_rej_n", len(rej))
            shown = rej[:3]
            tail = f" ...(共{total})" if total > len(shown) else ""
            lines.append(f"aspect_rej: {shown}{tail}")
        if stat.get("red_blob"):
            rb = stat["red_blob"]
            lines.append(f"red_blob: {rb['w']}x{rb['h']} area={rb['area']} "
                         f"h/w={rb['aspect']} fill={rb['fill']}")
        if stat.get("conf") is not None:
            thr = threshold if threshold is not None else params.get("min_confidence", _SC_MIN_CONF)
            bd = self._score_breakdown(stat.get("parts"), stat.get("conf"), thr)
            lines.append(f"打分: 总分{bd['总分']} 阈值{bd['阈值']} {'通过' if bd['通过'] else '不过'}")
            for it in bd["明细"]:
                lines.append(f"  {it['项']} 值{it['值']:.2f} ×{it['权重']} ={it['贡献']:.3f}")
            if stat.get("gap"):
                lines.append(f"gap: {stat.get('gap')}")
            if stat.get("proj") is not None:
                lines.append(f"proj: {stat.get('proj')}")
        return "\n".join(lines)

    def _dump_failure(self, node_name, roi_tuple, work_bgr, red_mask, inner_best):
        """失败常驻图：roi_crop + red_mask (+ inner)。固定名覆盖 + 时间节流防自循环刷屏。"""
        key = self._img_key(node_name, roi_tuple)
        last_map = getattr(self, "_last_dump", None)
        if last_map is None:
            last_map = self._last_dump = {}
        now = time.time()
        if now - last_map.get(key, 0.0) < _DUMP_MIN_INTERVAL:
            return {"status": "throttled", "files": []}
        last_map[key] = now

        saved = []
        for img, tag in ((work_bgr, "roi_crop"), (self._bool_to_bgr(red_mask), "red_mask")):
            p = self._save_debug_img(img, key, tag)
            if p:
                saved.append(p)
        if inner_best is not None:
            p = self._save_debug_img(self._bool_to_bgr(inner_best), key, "inner")
            if p:
                saved.append(p)
        if saved:
            print(f"[RedDotDetector] 失败截图 -> {saved}")  # 仅入 txt 日志，不上 UI
            return {"status": "saved", "files": saved}
        return {"status": "failed", "files": []}
