"""
商店购买 V3 - 单页收藏对齐动作

职责边界：已进入某卡带商店页面后，对当前页执行收藏对齐。

识别策略：
    - 商品名: 以星框为基准限定右侧 ROI；整页 OCR 经同一过滤表清洗后反查漏星
    - 星星位置: TemplateMatch method 5（颜色不敏感，黄灰都命中）
    - 星星颜色: numpy 对星星完整 box 采样，按高饱和度像素占比判色
      （黄星尖角高饱和 ~68%，灰星 ~0%，规避白色中心干扰）

数据来源（全部从 Pipeline 节点读取，Python 只留兜底默认值）：
    - 当前卡带名     ← custom_action_param（经 json.loads 解包，"推"入）
    - 购物清单       ← Data_Csm.attach[卡带名]
    - OCR 过滤词     ← Data_Csm.attach["ocr_exclude"]
    - 行为参数       ← Tuning_Csm.attach（延时/重试/方向范围/name_roi_offset）
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
from utils.arbitrage_purchase_lists import FORCED_UNFAVORITES, get_purchase_run
from utils.arbitrage_store import save_purchase_alignment, invalidate_purchase_alignment
from utils.account_sync import sync_from_context


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
# 星框的 [x, y, w, h] 偏移，限定右侧名称行；不预设货架格数。
NAME_ROI_OFFSET = [25, -6, 160, 12]

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
        run = None
        cart_name = None
        aligned = False
        self._scan_issues = []
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

            if not sync_from_context(context, where="ShopBuyFavController"):
                return False
            run = get_purchase_run(argv.task_detail.task_id)
            if run is None or cart_name not in run["table"]:
                raise ValueError("缺少本轮已准备采购名单，不能使用旧收藏")
            target_items = run["table"][cart_name] - FORCED_UNFAVORITES
            _, ocr_exclude = self._load_config(context, cart_name)
            if ocr_exclude is None:
                return False
            if not invalidate_purchase_alignment(cart_name):
                raise RuntimeError("旧收藏核实记录撤销失败，本卡带不执行点星")

            mfaalog.debug(
                f"[ShopBuy] 📋 [{cart_name}] 目标商品 ({len(target_items)}项): "
                f"{', '.join(target_items)}"
            )

            if self._align_favorites(context, target_items, ocr_exclude, cart_name):
                if not sync_from_context(context, where="ShopBuyFavController/complete"):
                    return False
                # 再次核对账号；失败或中途切换不能把结果写给其他账号。
                if get_purchase_run(argv.task_detail.task_id) is not run:
                    return False
                if not save_purchase_alignment(cart_name, target_items):
                    raise RuntimeError("收藏成功记录保存失败")
                aligned = True
            return aligned

        except Exception as e:
            mfaalog.error(f"[ShopBuy] ❌ 未预期异常: {e}")
            self._scan_issues.append(str(e))
            return False
        finally:
            if run is not None and isinstance(cart_name, str) and cart_name in run["table"]:
                # 只完成本卡带的尝试；失败不会撤销其他卡带，也不改总派发或购买节点。
                run["pending"].discard(cart_name)
                if aligned:
                    run["failed_cards"].pop(cart_name, None)
                else:
                    reason = "; ".join(self._scan_issues) or "收藏未核实"
                    run["failed_cards"][cart_name] = reason
                    mfaalog.warning(f"[ShopBuy] [{cart_name}] 本卡带结束：{reason}；继续下一张，下次仍需核对")

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
        offset = tuning.get("name_roi_offset", NAME_ROI_OFFSET)
        if (not isinstance(offset, (list, tuple)) or len(offset) != 4
                or any(type(value) is not int for value in offset)):
            raise ValueError("name_roi_offset必须是4个整数")
        cfg["name_roi_offset"] = list(offset)
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
            f"name_roi_offset={cfg['name_roi_offset']} "
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
                self._scan_issues = ["截图失败"]
                if attempt < max_retries:
                    time.sleep(verify_delay)
                    continue
                return False

            entities = self._scan_page(context, screenshot, ocr_exclude)
            if not self._complete_page(entities):
                # 本页尚未核实；已确认的天赋神药黄星仍单独取消。
                forced = [item for item in entities or [] if item["name"] in FORCED_UNFAVORITES]
                forced_actions = self._decide_actions(forced, set())
                if forced_actions:
                    self._execute_clicks(context, forced_actions, cart_name)
                mfaalog.warning(f"[ShopBuy] ⚠️ [{cart_name}] 商品未完整识别，不能核实收藏。")
                if attempt < max_retries:
                    time.sleep(verify_delay)
                    continue
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
            self._scan_issues = ["最终复核截图失败"]
            return False
        final_entities = self._scan_page(context, final_ss, ocr_exclude)
        if not self._complete_page(final_entities):
            return False
        final_actions = self._decide_actions(final_entities, target_items)
        if final_actions:
            self._scan_issues = ["星星仍未对齐：" + ", ".join(
                f"{a['name']}@({a['star_cx']:.0f},{a['star_cy']:.0f})" for a in final_actions)]
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

    def _complete_page(self, entities):
        """核对星名配对，允许同名多星；采购名单只决定亮灭，不规定种类或格数。"""
        return bool(entities) and not self._scan_issues

    # ==========================================
    # 页面扫描
    # ==========================================
    def _scan_page(self, context, screenshot, ocr_exclude):
        self._scan_issues = []
        # 所有定位都使用同一张图；局部覆盖只留在本次扫描的副本中。
        local = context.clone()
        page_names = self._read_names(local.run_recognition(NODE_OCR, screenshot), ocr_exclude)
        all_stars = self._read_stars(local.run_recognition(NODE_STAR, screenshot), screenshot)
        if not all_stars:
            self._scan_issues.append("未识别到星星")
            return []

        # 整页OCR只做反查。先排除过滤词，再用本页已观察到的行列约束排除图标杂字、升星说明等。
        # 不使用固定格数；固定取消收藏商品始终反查，不因目录或行列缺失而忽略。
        for name in page_names:
            if any(self._name_near_star(star, name) for star in all_stars):
                continue
            in_column = any(self.cfg["bind_dx_min"] <= name["left_x"] - star["right_x"]
                            <= self.cfg["bind_dx_max"] for star in all_stars)
            in_row = any(abs(name["cy"] - star["cy"]) <= self.cfg["bind_dy_max"] for star in all_stars)
            if name["name"] not in FORCED_UNFAVORITES and not (in_column and in_row):
                continue
            bw, bh = all_stars[0]["box"][2:]
            margin = self.cfg["bind_dy_max"]
            roi = [int(name["left_x"] - self.cfg["bind_dx_max"] - bw),
                   int(name["cy"] - margin - bh / 2),
                   int(self.cfg["bind_dx_max"] - self.cfg["bind_dx_min"] + bw), int(2 * margin + bh)]
            result = self._recognize_region(local, NODE_STAR, screenshot, roi)
            found = [star for star in self._read_stars(result, screenshot) if self._name_near_star(star, name)]
            if len(found) != 1:
                self._scan_issues.append(f"文字[{name['name']}] box={name['box']}向左反查到{len(found)}颗星")
                continue
            if not any(self._same_box(found[0]["box"], star["box"]) for star in all_stars):
                all_stars.append(found[0])
                mfaalog.info(f"[ShopBuy] 名称反查补回星星 [{name['name']}] box={found[0]['box']}")

        # 每颗星只读右侧名称行，允许同名出现在不同位置；多候选由配对检查明确拒绝。
        name_items = []
        for star in all_stars:
            roi = [value + offset for value, offset in zip(star["box"], self.cfg["name_roi_offset"])]
            result = self._recognize_region(local, NODE_OCR, screenshot, roi)
            names = self._read_names(result, ocr_exclude)
            for name in names:
                if self._name_near_star(star, name) and not any(
                        name["name"] == old["name"] and self._same_box(name["box"], old["box"])
                        for old in name_items):
                    name_items.append(name)
        entities = self._bind_star_to_name(all_stars, name_items)
        yellow_n = sum(star["color"] == "yellow" for star in all_stars)
        mfaalog.info(f"[ShopBuy] 星星{len(all_stars)}颗（黄{yellow_n}），明确配对{len(entities)}个，疑点{len(self._scan_issues)}项")
        for issue in self._scan_issues:
            mfaalog.warning(f"[ShopBuy] {issue}")
        return entities

    @staticmethod
    def _same_box(a, b):
        overlap = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
            0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        return overlap > 0.5 * min(a[2] * a[3], b[2] * b[3])

    def _read_names(self, result, ocr_exclude):
        names = []
        for match in (getattr(result, "all_results", None) or []):
            box, raw = getattr(match, "box", None), getattr(match, "text", None)
            if box is None or raw is None:
                continue
            text = canon(re.sub(r'[^\w一-龥]', '', raw))
            if not text or text.isdigit():
                continue
            if text not in FORCED_UNFAVORITES and (text in ocr_exclude or len(text) > self.cfg["name_max_len"]):
                continue
            box = list(box)
            if any(text == old["name"] and self._same_box(box, old["box"]) for old in names):
                continue
            names.append({"name": text, "box": box, "left_x": box[0], "cy": box[1] + box[3] / 2})
        return names

    def _read_stars(self, result, screenshot):
        img = np.asarray(screenshot)
        img_h, img_w = img.shape[:2]
        stars = []
        for match in (getattr(result, "filtered_results", None) or []):
            box = getattr(match, "box", None)
            if box is None:
                continue
            box = list(box)
            if any(self._same_box(box, star["box"]) for star in stars):
                continue
            x, y, w, h = box
            stars.append({"box": box, "cx": x + w / 2, "cy": y + h / 2, "right_x": x + w,
                          "color": self._classify_star_color(img, x, y, w, h, img_w, img_h)})
        return stars

    @staticmethod
    def _recognize_region(context, node, screenshot, roi):
        height, width = np.asarray(screenshot).shape[:2]
        x, y, w, h = roi
        x1, y1 = max(0, x), max(0, y)
        clipped = [x1, y1, min(width, x + w) - x1, min(height, y + h) - y1]
        if clipped[2] <= 0 or clipped[3] <= 0:
            return None
        return context.run_recognition(node, screenshot, {node: {"roi": clipped, "roi_offset": [0, 0, 0, 0]}})

    def _name_near_star(self, star, name):
        return (self.cfg["bind_dx_min"] <= name["left_x"] - star["right_x"] <= self.cfg["bind_dx_max"]
                and abs(name["cy"] - star["cy"]) <= self.cfg["bind_dy_max"])

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
        entities = []
        candidates = [[i for i, name in enumerate(name_items) if self._name_near_star(star, name)]
                      for star in all_stars]
        for star, indices in zip(all_stars, candidates):
            if len(indices) == 1 and sum(indices[0] in row for row in candidates) == 1:
                name_item = name_items[indices[0]]
                entities.append({
                    "name": name_item["name"],
                    "star_color": star["color"],
                    "star_cx": star["cx"],
                    "star_cy": star["cy"],
                })
                mfaalog.info(f"[ShopBuy] [{name_item['name']}] 名框={name_item['box']} "
                             f"星框={star['box']} 点击=({star['cx']:.0f},{star['cy']:.0f}) {star['color']}")
            else:
                self._scan_issues.append(f"星框={star['box']} 名称配对不明确："
                                         f"{[(name_items[i]['name'], name_items[i]['box']) for i in indices]}")
        return entities

    # ==========================================
    # 四分类决策
    # ==========================================
    def _decide_actions(self, entities, target_items):
        actions = []
        for entity in entities:
            name = entity["name"]
            color = entity["star_color"]
            is_target = name in target_items and name not in FORCED_UNFAVORITES

            if name in FORCED_UNFAVORITES:
                if color == "yellow":
                    actions.append({
                        "name": name, "action": "extinguish",
                        "star_cx": entity["star_cx"],
                        "star_cy": entity["star_cy"],
                    })
                    mfaalog.info(f"[ShopBuy]   🔄 [{name}] 固定取消收藏+黄星 → 将熄灭")
                else:
                    mfaalog.info(f"[ShopBuy]   ✓  [{name}] 固定取消收藏+灰星 → 已正确")
                continue

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
                return False
            cx = int(act["star_cx"])
            cy = int(act["star_cy"])
            verb = "点亮" if act["action"] == "light" else "熄灭"
            mfaalog.info(
                f"[ShopBuy]   👆 {i}/{len(actions)} "
                f"{verb} [{act['name']}] @ ({cx}, {cy})"
            )
            if not context.tasker.controller.post_click(cx, cy).wait().succeeded:
                mfaalog.warning(f"[ShopBuy] 点击未成功 [{act['name']}] @ ({cx}, {cy})，停止本批点击并重新核对")
                return False
            time.sleep(click_delay)
        return True
