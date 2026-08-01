"""
商店购买 V3 - 单页收藏对齐动作

职责边界：已进入某卡带商店页面后，对当前页执行收藏对齐。

识别策略：
    - 商品名: OCR（Pipeline 节点定义 ROI）
    - 星星位置: TemplateMatch method 5（颜色不敏感，黄灰都命中）
    - 星星颜色: numpy 对星星完整 box 采样，按高饱和度像素占比判色
      （黄星尖角高饱和 ~68%，灰星 ~0%，规避白色中心干扰）

数据来源（全部从 Pipeline 节点读取，Python 只留兜底默认值）：
    - 当前卡带名     ← custom_action_param（经 json.loads 解包，"推"入）
    - 购物清单       ← Data_Csm.attach[卡带名]
    - OCR 过滤词     ← Data_Csm.attach["ocr_exclude"]
    - 行为参数       ← Tuning_Csm.attach（延时/重试/几何配对窗口）
    - 商品名         ← ReadNames_Csm 节点
    - 商品名长度阈   ← ReadNames_Csm.attach["name_max_len"]
    - 星星位置       ← FindStars_Csm 节点
    - 判色参数       ← FindStars_Csm.attach（sat 阈值/星心内缩比）
    



参数外置说明（[2026-07-22]）：
    原先散落在本文件的数值常量已迁往上述节点的 attach，各端可经资源覆盖
    （base→pc→…，attach 按 key 字典合并）独立调参而不动 Python。本文件保留
    的同名常量仅作「节点缺失 / key 缺失」时的兜底默认值。
"""

import json
import re
import time
import numpy as np
from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer
from utils import mfaalog
from utils.name_i18n import canon


# 数据节点名（py 自定义引用节点，_Csm 后缀标记）
DATA_NODE   = "Arbitrage_ShopBuy_Data_Csm"
NODE_TUNING = "Arbitrage_ShopBuy_Tuning_Csm"

# 识别节点名
NODE_OCR  = "Arbitrage_ShopBuy_ReadNames_Csm"
NODE_STAR = "Arbitrage_ShopBuy_FindStars_Csm"

# ------------------------------------------------------------------
# 以下均为兜底默认值：正常从对应节点 attach 读取，读不到才回落到这里。
# 分组与顺序对齐 base 的 _Csm 节点块 2/4~4/4，便于三处（常量↔_load_params↔JSON）对照。
# ------------------------------------------------------------------

# ← Tuning_Csm.attach（2/4·控制器行为参数）
# 点击间延迟（秒），等待菱形光特效消散
CLICK_DELAY = 1.5
# 验证前等待（秒），等待 Toast 消息淡出
VERIFY_DELAY = 2.0
# 单页对齐重试次数
MAX_RETRIES = 1
# 星-名几何配对窗口（星星右边缘 → 商品名左边缘）
BIND_DX_MIN = 5    # 商品名至少在星星右侧 5px
BIND_DX_MAX = 40   # 最远不超过 40px
BIND_DY_MAX = 15   # Y 轴差距不超过 15px

# ← ReadNames_Csm.attach（3/4·名识别参数）
# 商品名最大长度（中文字符数），过滤掉 Toast 消息。
# 取 7 与 ReadNames_Csm.attach.name_max_len 对齐：商店里确有 7 字商品（当前都不是
# 购买对象，属注入冗余）。两处必须同值，否则 attach 读失败回落时会多滤掉一截长名。
NAME_MAX_LEN = 7

# ← FindStars_Csm.attach（4/4·判色参数）
# 黄星尖角像素饱和度 >0.3 占比约 68%，灰星约 0%；阈值 15% 居中分离
SAT_PIXEL_THRESHOLD = 0.3   # 单像素饱和度阈值
SAT_RATIO_THRESHOLD = 0.15  # 高饱和像素占比阈值
# 星框四边内缩比：0 = 全框采样（安卓基线）。PC 因商品图缩小、框角渗入暖色卡面
# 艺术背景，需覆盖为 0.3 只采星心核以排除背景（见 FindStars_Csm.attach 的 pc 覆盖）。
STAR_CORE_INSET = 0.0


@AgentServer.custom_action("ShopBuyFavController")
class ShopBuyFavController(CustomAction):

    # ==========================================
    # 主入口
    # ==========================================
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            cart_name = argv.custom_action_param
            if isinstance(cart_name, str):
                try:
                    cart_name = json.loads(cart_name)
                except (json.JSONDecodeError, TypeError):
                    pass

            if not cart_name or not isinstance(cart_name, str):
                mfaalog.warning("[ShopBuy] ⚠️ 未收到卡带名，中止。")
                return False

            mfaalog.info(f"[ShopBuy] 🛒 收藏对齐启动 → 卡带 [{cart_name}]")

            # 先加载外置参数（各端可覆盖），失败自动回落兜底默认值。
            self.cfg = self._load_params(context)

            target_items, ocr_exclude = self._load_config(context, cart_name)
            if target_items is None:
                return False
            if not target_items:
                mfaalog.info(
                    f"[ShopBuy] [{cart_name}] 购物清单为空，跳过购买。"
                )
                return True

            mfaalog.debug(
                f"[ShopBuy] 📋 [{cart_name}] 目标商品 ({len(target_items)}项): "
                f"{', '.join(target_items)}"
            )

            return self._align_favorites(
                context, target_items, ocr_exclude, cart_name
            )

        except Exception as e:
            mfaalog.error(f"[ShopBuy] ❌ 未预期异常: {e}")
            return False

    # ==========================================
    # 参数读取（外置 attach + 兜底默认值）
    # ==========================================
    def _read_attach(self, context, node_name) -> dict:
        """安全读取某节点 attach，读不到返回空 dict。"""
        try:
            node = context.get_node_object(node_name)
        except Exception:
            node = None
        attach = getattr(node, 'attach', None) if node else None
        return attach if isinstance(attach, dict) else {}

    @staticmethod
    def _cast(attach: dict, key, default, cast):
        """从 attach 取 key 并转型；缺失/坏值回落 default。"""
        if key not in attach:
            return default
        try:
            return cast(attach[key])
        except (TypeError, ValueError):
            return default

    def _load_params(self, context) -> dict:
        tuning = self._read_attach(context, NODE_TUNING)
        name   = self._read_attach(context, NODE_OCR)
        star   = self._read_attach(context, NODE_STAR)

        cfg = {
            # 2/4 行为参数 ← Tuning_Csm
            "click_delay":  self._cast(tuning, "click_delay",  CLICK_DELAY,  float),
            "verify_delay": self._cast(tuning, "verify_delay", VERIFY_DELAY, float),
            "max_retries":  self._cast(tuning, "max_retries",  MAX_RETRIES,  int),
            "bind_dx_min":  self._cast(tuning, "bind_dx_min",  BIND_DX_MIN,  int),
            "bind_dx_max":  self._cast(tuning, "bind_dx_max",  BIND_DX_MAX,  int),
            "bind_dy_max":  self._cast(tuning, "bind_dy_max",  BIND_DY_MAX,  int),
            # 3/4 名识别参数 ← ReadNames_Csm
            "name_max_len": self._cast(name, "name_max_len", NAME_MAX_LEN, int),
            # 4/4 判色参数 ← FindStars_Csm
            "sat_pixel_threshold": self._cast(star, "sat_pixel_threshold", SAT_PIXEL_THRESHOLD, float),
            "sat_ratio_threshold": self._cast(star, "sat_ratio_threshold", SAT_RATIO_THRESHOLD, float),
            "star_core_inset":     self._cast(star, "star_core_inset",     STAR_CORE_INSET,     float),
        }
        # inset 越界只告警、不改值：判据是几何事实而非调参经验——四边各内缩 inset 比例后，
        # 采样区宽高占比 = 1-2×inset，inset≥0.5 时它 ≤0，星心核塌成 0 像素。
        inset = cfg["star_core_inset"]
        if not 0.0 <= inset < 0.5:
            mfaalog.warning(
                f"[ShopBuy] ⚠️ star_core_inset={inset:.2f} 越界，有效范围 0 ≤ inset < 0.5"
                f"（0=全框采样；0.3 左右=只采星心核，用于排除框角渗入的卡面暖色背景）。"
                f"当前取值会让采样区塌成 0 像素 → 所有星一律判灰 → _decide_actions 认为"
                f"每个目标商品都需点亮 → 点亮/熄灭振荡直到重试耗尽。"
                f"请修正 {NODE_STAR}.attach.star_core_inset；本轮按原值继续。"
            )
        mfaalog.info(
            "[ShopBuy] 🔧 参数: "
            f"inset={cfg['star_core_inset']:.2f} "
            f"sat>{cfg['sat_pixel_threshold']:.2f}占比>{cfg['sat_ratio_threshold']:.0%} "
            f"name_max={cfg['name_max_len']} "
            f"dx[{cfg['bind_dx_min']},{cfg['bind_dx_max']}] dy≤{cfg['bind_dy_max']} "
            f"click={cfg['click_delay']}s verify={cfg['verify_delay']}s "
            f"retry={cfg['max_retries']}"
        )
        return cfg

    # ==========================================
    # 配置读取
    # ==========================================
    def _load_config(self, context: Context, cart_name: str):
        node_obj = context.get_node_object(DATA_NODE)
        if not node_obj or not getattr(node_obj, 'attach', None):
            mfaalog.warning(
                f"[ShopBuy] ❌ 无法读取 [{DATA_NODE}] 的 attach。"
                f"提示：enabled:false 的节点可能无法访问。"
            )
            return None, None

        attach = node_obj.attach

        items_str = attach.get(cart_name)
        if items_str is None or not isinstance(items_str, str):
            mfaalog.warning(
                f"[ShopBuy] ⚠️ 未找到卡带 [{cart_name}] 的购物清单。"
            )
            return None, None

        exclude_str = attach.get("ocr_exclude", "")
        ocr_exclude = (
            self._parse_item_list(exclude_str) if exclude_str else set()
        )
        mfaalog.info(
            f"[ShopBuy] 🔧 ocr_exclude ({len(ocr_exclude)}项): "
            f"{ocr_exclude if ocr_exclude else '空！'}"
        )

        if not items_str:
            # 空字符串 = 有意配置为"无需购买"，返回空集合
            return set(), ocr_exclude

        target_items = self._parse_item_list(items_str)
        if not target_items:
            mfaalog.warning(f"[ShopBuy] ⚠️ [{cart_name}] 购物清单解析为空。")
            return None, None

        return target_items, ocr_exclude

    def _parse_item_list(self, raw_str: str) -> set:
        raw_items = [
            x.strip()
            for x in re.split(r'[，,;|]+', raw_str)
            if x.strip()
        ]
        cleaned = set()
        for item in raw_items:
            c = re.sub(r'[^\w一-龥]', '', item)
            if c:
                # 归一化到规范简体：清单可用简/繁书写，统一后与 OCR 名同域比较。
                cleaned.add(canon(c))
        return cleaned

    # ==========================================
    # 单页对齐
    # ==========================================
    def _align_favorites(
        self, context, target_items, ocr_exclude, cart_name
    ) -> bool:
        max_retries = self.cfg["max_retries"]
        verify_delay = self.cfg["verify_delay"]
        for attempt in range(1 + max_retries):
            if context.tasker.stopping:
                return False

            label = "初次" if attempt == 0 else f"重试第{attempt}次"
            mfaalog.info(f"[ShopBuy] 🔍 [{cart_name}] {label}扫描...")

            screenshot = (
                context.tasker.controller.post_screencap().wait().get()
            )
            if screenshot is None:
                mfaalog.warning("[ShopBuy] ❌ 截图失败。")
                return False

            entities = self._scan_page(context, screenshot, ocr_exclude)
            if entities is None:
                mfaalog.warning(f"[ShopBuy] ⚠️ [{cart_name}] 识别失败。")
                return False

            actions = self._decide_actions(entities, target_items)
            if not actions:
                mfaalog.info(
                    f"[ShopBuy] ✨ [{cart_name}] 收藏状态已正确，无需操作。"
                )
                return True

            if attempt > 0:
                mfaalog.warning(
                    f"[ShopBuy] ⚠️ [{cart_name}] "
                    f"仍有 {len(actions)} 项未对齐，重试..."
                )

            self._execute_clicks(context, actions, cart_name)

            if attempt < max_retries:
                mfaalog.info(
                    f"[ShopBuy] 🔁 [{cart_name}] "
                    f"等待 {verify_delay}s 后验证..."
                )
                time.sleep(verify_delay)

        # 最终验证
        if context.tasker.stopping:
            return False
        mfaalog.info(f"[ShopBuy] 🔎 [{cart_name}] 最终验证...")
        time.sleep(verify_delay)

        final_ss = context.tasker.controller.post_screencap().wait().get()
        if final_ss is None:
            return False
        final_entities = self._scan_page(context, final_ss, ocr_exclude)
        if final_entities is None:
            return False
        final_actions = self._decide_actions(final_entities, target_items)
        if final_actions:
            mfaalog.warning(
                f"[ShopBuy] ❌ [{cart_name}] 最终验证仍有 "
                f"{len(final_actions)} 项未对齐: "
                + ", ".join(
                    f"{a['name']}({'需点亮' if a['action']=='light' else '需熄灭'})"
                    for a in final_actions
                )
            )
            return False

        mfaalog.info(f"[ShopBuy] ✅ [{cart_name}] 收藏对齐验证通过！")
        return True

    # ==========================================
    # 页面扫描
    # ==========================================
    def _scan_page(self, context, screenshot, ocr_exclude):

        name_max_len = self.cfg["name_max_len"]

        # --- OCR 商品名 ---
        ocr_result = context.run_recognition(NODE_OCR, screenshot)
        if not ocr_result or not ocr_result.all_results:
            mfaalog.warning("[ShopBuy] OCR 未识别到任何文本。")
            return None

        name_items = []
        for match in ocr_result.all_results:
            box = getattr(match, 'box', None)
            text = getattr(match, 'text', None)
            if box is None or text is None:
                continue
            x, y, w, h = box
            cleaned = re.sub(r'[^\w一-龥]', '', text)
            # 归一化到规范简体：繁体端 OCR 读到的繁体名（含跨版本异义词）在此折叠为
            # 简体，之后的 ocr_exclude/长度阈/与 target_items 比较全在同一简体域进行。
            # 名字只用于判定与按坐标点星，不回填 UI，归一化安全（对比卖出侧的约束）。
            cleaned = canon(cleaned)
            if not cleaned or cleaned.isdigit():
                continue
            if cleaned in ocr_exclude:
                continue
            # 过滤 Toast 消息（"已将商品蘑菇加入收藏"等长文本）
            if len(cleaned) > name_max_len:
                continue
            name_items.append({
                "name": cleaned,
                "left_x": x,
                "cy": y + h / 2,
            })

        mfaalog.info(f"[ShopBuy] OCR 过滤后: {len(name_items)} 项")
        for item in name_items:
            mfaalog.info(
                f"  {item['name']:6s} "
                f"left_x={item['left_x']:.0f} cy={item['cy']:.0f}"
            )

        if not name_items:
            mfaalog.warning("[ShopBuy] OCR 清洗后无有效商品名。")
            return None

        # --- 星星位置 ---
        star_result = context.run_recognition(NODE_STAR, screenshot)
        if not star_result or not star_result.filtered_results:
            mfaalog.warning("[ShopBuy] 未识别到任何星星。")
            return None

        img = np.asarray(screenshot)
        img_h, img_w = img.shape[:2]

        all_stars = []
        for match in star_result.filtered_results:
            box = getattr(match, 'box', None)
            if box is None:
                continue
            bx, by, bw, bh = box
            color = self._classify_star_color(
                img, bx, by, bw, bh, img_w, img_h
            )
            all_stars.append({
                "box": [bx, by, bw, bh],
                "cx": bx + bw / 2,
                "cy": by + bh / 2,
                "right_x": bx + bw,
                "color": color,
            })

        yellow_n = sum(1 for s in all_stars if s["color"] == "yellow")
        gray_n = len(all_stars) - yellow_n
        mfaalog.info(
            f"[ShopBuy] 星星: {len(all_stars)} 个 "
            f"({yellow_n} 黄, {gray_n} 灰)"
        )

        if not all_stars:
            return None

        # --- 配对 ---
        entities = self._bind_star_to_name(all_stars, name_items)
        if not entities:
            mfaalog.warning("[ShopBuy] ⚠️ 星星与商品名完全无法配对，识别失败。")
            return None
        return entities

    # ==========================================
    # 星星颜色判定（星心核 + 高饱和像素占比）
    # ==========================================
    def _classify_star_color(
        self, img, bx, by, bw, bh, img_w, img_h
    ) -> str:
        """
        采样星星 box（四边各内缩 star_core_inset 比例），计算饱和度 > sat_pixel
        的像素占比，占比 > sat_ratio 判黄，否则判灰。三参数均从 FindStars_Csm.attach
        读取，缺失回落兜底默认。

        内缩比 star_core_inset 外置到 attach（避免把两端差值硬编码进 py），当前
        base/pc 两端均取 0.0=全框采样（安卓标定黄~68%/灰~0%）。

        【调参】识别框角部若渗入卡面暖色艺术背景（如米=金色稻草），灰星全框高饱和
        占比可能越过 sat_ratio 阈被误判黄 → 对齐循环点亮/熄灭振荡。此时把对应端
        FindStars_Csm.attach 的 star_core_inset 调到 0.3 左右改采星心核即可排除
        背景；星心=纯星体填充，调高不损判别力。
        """
        inset = self.cfg["star_core_inset"]
        sat_pixel = self.cfg["sat_pixel_threshold"]
        sat_ratio = self.cfg["sat_ratio_threshold"]

        inset_x = int(bw * inset)
        inset_y = int(bh * inset)
        x1 = max(0, bx + inset_x)
        y1 = max(0, by + inset_y)
        x2 = min(img_w, bx + bw - inset_x)
        y2 = min(img_h, by + bh - inset_y)

        patch = img[y1:y2, x1:x2, :3].astype(np.float32)
        if patch.size == 0:
            # 采样区空 = 判色失去依据。仍返回 "gray" 保持调用方形态，但绝不能闷声返回：
            # 这条日志是「全员判灰→点亮/熄灭振荡」现场的唯一线索。每页多颗星，只报一次。
            if not getattr(self, "_warned_empty_patch", False):
                self._warned_empty_patch = True
                mfaalog.warning(
                    f"[ShopBuy] ⚠️ ({bx},{by}) 星心核采样区为空 "
                    f"(inset={inset:.2f} 内缩后 {x1},{y1}→{x2},{y2}，星框 {bw}×{bh})，"
                    f"本页所有星将一律判灰、触发点亮/熄灭振荡。"
                    f"见上方 {NODE_STAR}.attach.star_core_inset 告警。"
                )
            return "gray"

        max_ch = patch.max(axis=2)
        min_ch = patch.min(axis=2)
        safe_max = np.where(max_ch > 0, max_ch, 1.0)
        saturation = (max_ch - min_ch) / safe_max

        high_sat_ratio = float((saturation > sat_pixel).mean())

        result = "yellow" if high_sat_ratio > sat_ratio else "gray"
        mfaalog.info(
            f"[ShopBuy]   🎨 ({bx},{by}) "
            f"high_sat={high_sat_ratio:.0%} → {result}"
        )
        return result

    # ==========================================
    # 星星→商品名 配对
    # ==========================================
    def _bind_star_to_name(self, all_stars, name_items):
        dx_min = self.cfg["bind_dx_min"]
        dx_max = self.cfg["bind_dx_max"]
        dy_max = self.cfg["bind_dy_max"]

        entities = []
        used_names = set()

        for star in all_stars:
            best_name = None
            best_dx = float('inf')

            for i, name_item in enumerate(name_items):
                if i in used_names:
                    continue
                dx = name_item["left_x"] - star["right_x"]
                dy = abs(name_item["cy"] - star["cy"])
                if dx_min <= dx <= dx_max and dy <= dy_max:
                    if dx < best_dx:
                        best_dx = dx
                        best_name = (i, name_item)

            if best_name:
                idx, name_item = best_name
                used_names.add(idx)
                entities.append({
                    "name": name_item["name"],
                    "star_color": star["color"],
                    "star_cx": star["cx"],
                    "star_cy": star["cy"],
                })
                mfaalog.info(
                    f"[ShopBuy]   🔗 [{name_item['name']}] "
                    f"↔ 星({star['right_x']:.0f},{star['cy']:.0f}) "
                    f"dx={best_dx:.0f} {star['color']}"
                )
            else:
                mfaalog.warning(
                    f"[ShopBuy] ⚠️ 星星 box={star['box']} 未配对到商品名"
                )

        return entities

    # ==========================================
    # 四分类决策
    # ==========================================
    def _decide_actions(self, entities, target_items):
        actions = []
        for entity in entities:
            name = entity["name"]
            color = entity["star_color"]
            is_target = name in target_items

            if is_target and color == "gray":
                actions.append({
                    "name": name, "action": "light",
                    "star_cx": entity["star_cx"],
                    "star_cy": entity["star_cy"],
                })
                mfaalog.info(f"[ShopBuy]   ⭐ [{name}] 目标+灰星 → 将点亮")
            elif not is_target and color == "yellow":
                actions.append({
                    "name": name, "action": "extinguish",
                    "star_cx": entity["star_cx"],
                    "star_cy": entity["star_cy"],
                })
                mfaalog.info(f"[ShopBuy]   🔄 [{name}] 非目标+黄星 → 将熄灭")
            elif is_target and color == "yellow":
                mfaalog.info(f"[ShopBuy]   ✓  [{name}] 目标+黄星 → 已正确")

        return actions

    # ==========================================
    # 执行点击
    # ==========================================
    def _execute_clicks(self, context, actions, cart_name):
        click_delay = self.cfg["click_delay"]
        actions.sort(key=lambda a: (a["star_cy"], a["star_cx"]))
        mfaalog.info(
            f"[ShopBuy] 🎯 [{cart_name}] "
            f"共 {len(actions)} 个星星待点击..."
        )

        for i, act in enumerate(actions, 1):
            if context.tasker.stopping:
                break
            cx = int(act["star_cx"])
            cy = int(act["star_cy"])
            verb = "点亮" if act["action"] == "light" else "熄灭"
            mfaalog.info(
                f"[ShopBuy]   👆 {i}/{len(actions)} "
                f"{verb} [{act['name']}] @ ({cx}, {cy})"
            )
            context.tasker.controller.post_click(cx, cy).wait()
            time.sleep(click_delay)
