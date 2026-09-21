import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer
from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_store import (
    get_market_snapshot,
    market_day,
    save_market_snapshot,
    save_possession_snapshot,
)
from utils.name_i18n import canon, name_variants
from utils.arbitrage_pricelist import (
    read_name as _read_item_name,
    read_prices as _read_prices,
    load_config as _load_price_cfg,
    confirm_prices as _confirm_prices,
    price_issues as _price_issues,
    money_value as _parse_money_value,
)
from utils.arbitrage_material_policy import read_material_reserve_policy
from utils.arbitrage_cartridge import (
    DEFAULT_CONFIG as _RESCUE_CFG,
    RESCUE_NODE as _RESCUE_NODE,
    classify_type as _classify_cart_type,
    load_config as _load_rescue_cfg,
    read_current as _read_cartridge,
    rescue_rois as _rescue_rois,
    rescue_tail_num as _rescue_tail_num,
)
from action.arbitrage_sell_batch import execute_sale_item

# ==========================================
# 三列各自窄 roi OCR(#Q2.5,2026-07-24)：名/价/卡带在各自节点的 roi 内分别识别。
# 窄 roi 让小字(卡带尾号)可靠——大 roi 整表 OCR 会漏检小数字(07-24实录:活動卡的号
# 在整表 ReadList 里检不出,单独窄 roi 一放大就认出)。roi 由各节点 JSON 承载,
# run_recognition 自动生效,故不再需要 get_node_data 读列带、也无需 cx 过滤分列。
# ==========================================
_COL_NAME = "Arbitrage_Sell_Col_Name"
_COL_AMOUNT = "Arbitrage_Sell_Col_Amount"
_COL_PRICE = "Arbitrage_Sell_Col_Price"
_COL_CART = "Arbitrage_Sell_Col_Cart"
_SELL_ITEM_LIST = "Arbitrage_Sell_Item_ListTraverse"
_SELL_ITEM_OCR = "Agt_<Sell_Item>_Ocr"
_SELL_ITEM_OCR_SCORE = "Agt_<Sell_Item>_OcrScore"
_SELL_ITEM_TEMPLATE = "Agt_<Sell_Item>_Tmp"
_SELL_QUANTITY = "Arbitrage_Sell_Item_Quantity"

# ==========================================
# 子行断界(2026-07-24,#Q2)：价目表每个商品占两行
#   上子行 = 今天的实际行情(溢价率/该去哪个卡带卖) —— 与商品名同高
#   下子行 = 该商品每月最高价日的行情(仅供比对,不可当目标)
# 断界不再读「当前/每月」文字(语言相关,繁/简/英/日各异,曾是硬编码语义依赖),改纯几何:
#   名锚 y 即上子行 y;同一商品带(名锚 i → 名锚 i+1)内、名字下方最近的价格行即下子行。
# 名锚分带令每个价格文本只归属唯一商品,结构性根除跨行假交集(旧赤道法分配阈值50px>行距
# 73px,上一商品的下子行被吸进本行凑假交集误判满价,07-22实录:流浪美食家/桑格利亚酒);
# 带内按 y 升序天然区分上/下,无需任何语言标记。
# ==========================================
# 溢价率取两三位数+%:排除OCR把装饰符读成"4"/"A"的噪声,并吃'18120%'粘连(取靠%的三位)
RE_PCT = re.compile(r'(\d{2,3})\s*%')
RE_MONEY = re.compile(r'(?:[0-9]+|[0-9]{1,3}(?P<sep>[,.])[0-9]{3}(?:(?P=sep)[0-9]{3})*)')
SUBROW_TOL = 14      # 同子行 y 容差(子行间距约30px,商品行距约73px)
SCORE_MIN = 0.6      # 卡带选中组组分低于此=低置信,打WRN(实录错读曾得0.51,正确读数更高,#B)


# 卖出验证(2026-08-06 改)：金币不再由主控在派发前后自测,改由链内 A/B 两个动作节点测量,
# 主控只取结论 —— 时序契约见 gold_verify.py。主控那对读数跨越整条出售链,时间窗长且看不见
# 链条内部走到哪一步、卖了几件;链内测点贴着出售动作,还能覆盖连续出售的多件累计。
# run_task 成功只证明框架返回；实际成交由报价等额金币证据或一次库存回读确认。


def _sell_item_override(context: Context, item_name: str) -> dict:
    """商品全名OCR按置信度择优，未命中才试模板；不改变卡带匹配顺序。"""
    result = {
        _SELL_ITEM_OCR: {"expected": ["^" + re.escape(name) + "$" for name in name_variants(item_name)]},
        _SELL_ITEM_LIST: {"any_of": [_SELL_ITEM_OCR_SCORE]},
    }
    try:
        node = context.get_node_object(_SELL_ITEM_TEMPLATE)
        attach = getattr(node, "attach", None) or {}
        templates = attach.get("templates", {})
        matched = [value for key, value in templates.items() if canon(key) == canon(item_name)]
        if len(matched) != 1:
            return result
        paths = matched[0]
        paths = [paths] if isinstance(paths, str) else paths
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) or not path.strip() for path in paths)):
            raise ValueError("模板配置必须是非空路径或路径列表")
        result[_SELL_ITEM_TEMPLATE] = {"template": list(paths)}
        result[_SELL_ITEM_LIST]["any_of"].append(_SELL_ITEM_TEMPLATE)
    except Exception as exc:
        mfaalog.warning(f"[Arbitrage] [{item_name}] 出售模板配置不可用，仅使用OCR: {exc}")
    return result


def _task_ok(detail) -> bool:
    """run_task 的返回值判成败。

    它返回 Optional[TaskDetail],而 **TaskDetail 对象恒为真值** —— 直接 `if detail`
    只测得出"任务提没提上去",测不出"它跑成没跑成"。本文件已因此栽过两次(翻页段把
    TaskDetail 当 bool 导致"滑不动"完全不可见;派发段 `elif sell_result` 让失败分支
    几乎不可达),故收成一处,新代码一律走这里。
    """
    return detail is not None and detail.status.succeeded


def _verdict_rank(verdict) -> int:
    """跨候选比"哪个结论更强"。数越大越强,同强度保留先到的那个。

    2(卖成了) > 1(确凿没卖成) > 0(B 跑了但读数缺一端) > -1(B 压根没执行)。

    要点在于**只升不降**:候选 1 读到确凿的「没卖出」、候选 2 的 B 却没跑成时,不能让
    后者的空结论抹掉前者 —— 否则这项漏进"无法判断",汇总还会误报「全部无法验证」。
    """
    if verdict is None:
        return -1
    delta = verdict.get("delta")
    if delta is None:
        return 0
    return 2 if delta > 0 else 1


# OCR 同趟里对繁简会来回读(07-24实录同次结果 帶/带 混用),这几个字互吃
_CART_FUZZ = {'帶': '[帶带]', '带': '[帶带]', '遊': '[遊游]', '游': '[遊游]',
              '戲': '[戲戏]', '戏': '[戲戏]'}


def _cart_expected(raw: str) -> str:
    """卡带整串 → 选卡带菜单匹配式。类型逐字取自实读(活動/故事/角色/剧情… 皆可,不写死),
    仅对 OCR 繁简来回读的字互吃;号精确 (?<!\\d)N(?!\\d) 防 7 误配 17/71。
    菜单侧空格由节点 replace 去除;类型的 OCR 错字(如剧→则)属异常,留后续插件。"""
    m = re.search(r'(\d+)\s*$', raw)
    if not m:
        return raw            # 无号(提取正常应带号):退回整串,菜单按类型匹配
    body = ''.join(_CART_FUZZ.get(c, re.escape(c)) for c in raw[:m.start()])
    return body + r'(?<!\d)' + m.group(1) + r'(?!\d)'


# 当前和月度卡带按位置分别读取。金额因取整相等不代表两行是同一个柜台。
def _cart_group(dets) -> tuple:
    """区域内先按行、再按横向位置拼接，支持类型和编号换行。"""
    dets = sorted(dets, key=lambda t: t["cy"])
    if not dets:
        return "", 0.0
    lines = []
    for det in dets:
        if lines and abs(det["cy"] - lines[-1][0]["cy"]) < min(det["h"], lines[-1][0]["h"]) / 2:
            lines[-1].append(det)
        else:
            lines.append([det])
    raw = "".join(t["text"] for line in lines for t in sorted(line, key=lambda t: t["cx"]))
    text = re.sub(r'[^\w一-龥]', '', raw)
    return text, min(t["score"] for t in dets)


# ==========================================
# 尾号救援：缺号或编号换行时，在当前区域内改变裁剪边界，only_rec跳过检测直接读编号。
# ROI沿用类型框右对齐的宽/窄及纵向偏移配置，限定在当前区域并去重。
# 至少两个不同裁剪读到相同编号才接受；9/19等分歧不按置信度选胜者。
# ==========================================
# 扫描翻页的硬上界。业务可用节点的 custom_action_param 传 max_scan_pages 覆盖,
# 但不允许无限翻 —— 见 run() 里三层终止条件的说明。
_MAX_SCAN_PAGES_DEFAULT = 30

_MODE_SELL = "sell"
_MODE_PREVIEW_ALL = "preview_all"
_MODE_PREVIEW_POSSESS = "preview_possess"
_VALID_MODES = {_MODE_SELL, _MODE_PREVIEW_ALL, _MODE_PREVIEW_POSSESS}

_ITEM_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "bd2_item_names_i18n.json"
_RECIPE_NAMES = None

def _tail_num(s: str) -> str:
    """整串尾部连续数字(尾号);无则空串。"""
    m = re.search(r'(\d+)\s*$', s)
    return m.group(1) if m else ""


def _money_token_value(text: str) -> int | None:
    """价目表整数金额：允许边缘图标杂字，不合并多个数字段或猜测残缺数字。"""
    return _parse_money_value(text)


def _max_price_verdict(top_money: set[int], bot_money: set[int],
                       top_pct: set[str], bot_pct: set[str]) -> tuple[bool, str]:
    """金额证据优先；两侧金额都读到但矛盾时，不允许溢价率把它覆盖。"""
    if top_money and bot_money:
        if len(top_money) != 1 or len(bot_money) != 1:
            return False, "unconfirmed_amount"
        return top_money == bot_money, "amount"
    if top_pct and bot_pct:
        return bool(top_pct & bot_pct), "rate_fallback"
    return False, "unreadable"


def _cart_groups(carts):
    """纯编号连接邻近类型；保留原框，以类型框的位置决定整组归属。"""
    groups = [[d] for d in carts if re.search(r'[^\W\d_]', d['text'])]
    orphans = []
    for number in sorted(carts, key=lambda d: (d['cy'], d['cx'])):
        if not re.fullmatch(r'\s*[0-9]+\s*', number['text']):
            continue
        candidates = []
        for group in groups:
            typed = group[0]
            dy = number['cy'] - typed['cy']
            overlap = min(number['x'] + number['w'], typed['x'] + typed['w']) - max(number['x'], typed['x'])
            same_line = (abs(dy) <= min(number['h'], typed['h']) / 2
                         and typed['x'] + typed['w'] - number['w'] / 4 <= number['x']
                         <= typed['x'] + typed['w'] + typed['h'])
            wrapped = (min(number['h'], typed['h']) / 2 < dy <= 1.5 * typed['h']
                       and overlap > 0)
            if same_line or wrapped:
                candidates.append((abs(dy), group))
        candidates.sort(key=lambda pair: pair[0])
        if (not candidates or len(candidates) > 1 and candidates[0][0] == candidates[1][0]
                or len(candidates[0][1]) != 1 or _tail_num(candidates[0][1][0]['text'])):
            orphans.append(number)
            continue
        candidates[0][1].append(number)
    return groups, orphans


def _cart_split_y(names, amounts, current_y, monthly_y, next_y):
    """复用现有OCR：名称/基础金额中线优先，当前/月峰值收购金额中线备用。"""
    bases = [d for d in names if current_y + SUBROW_TOL < d['cy'] < next_y
             and re.fullmatch(r'[\s0-9,.]+', d['text']) and _money_token_value(d['text']) is not None]
    if len(bases) == 1:
        return (current_y + bases[0]['cy']) / 2
    if monthly_y is None:
        return None
    current = [d for d in amounts if abs(d['cy'] - current_y) <= SUBROW_TOL
               and _money_token_value(d['text']) is not None]
    monthly = [d for d in amounts if abs(d['cy'] - monthly_y) <= SUBROW_TOL
               and _money_token_value(d['text']) is not None]
    if len(current) == len(monthly) == 1 and current[0]['cy'] < monthly[0]['cy']:
        return (current[0]['cy'] + monthly[0]['cy']) / 2
    return None


def _cart_regions(carts, current_y, monthly_y, next_y, column_roi, *, split_y=None, previous_monthly_y=None):
    """先拼类型/编号，再按类型中心分当前/月度；裁剪边界独立保留完整编号。"""
    if split_y is None:
        if monthly_y is None or not current_y < monthly_y < next_y:
            return [], [], None
        split_y = (current_y + monthly_y) / 2
    if not current_y < split_y < next_y:
        return [], [], None
    x, y, w, h = column_roi
    monthly_y = monthly_y if monthly_y is not None else 2 * split_y - current_y
    if previous_monthly_y is None:
        previous_monthly_y = monthly_y - (next_y - current_y)
    upper = max(y, (previous_monthly_y + current_y) / 2)
    lower = min(y + h, (monthly_y + next_y) / 2)
    if not upper < split_y < lower:
        return [], [], None
    groups, orphans = _cart_groups(carts)
    day_groups = [g for g in groups if upper <= g[0]['cy'] < split_y]
    month_groups = [g for g in groups if split_y <= g[0]['cy'] < lower]
    # 中线负责文字归属；月度类型顶部才是编号复核不能越过的下界。
    crop_bottom = min(g[0]['y'] for g in month_groups) if month_groups else split_y
    next_types = [g[0]['y'] for g in groups if g[0]['cy'] >= lower]
    month_bottom = min(next_types) if next_types else y + h
    if crop_bottom <= upper:
        return [], [], None
    current = [d for g in day_groups for d in g if d['y'] + d['h'] <= crop_bottom]
    monthly = [d for g in month_groups for d in g if d['y'] + d['h'] <= month_bottom]
    if not day_groups:
        current = [d for d in orphans if upper <= d['cy'] < split_y]
    return current, monthly, (x, upper, x + w, crop_bottom)


def _current_cart(dets, context, screenshot, cfg, bounds, label):
    """Compatibility entry for standalone cartridge checks."""
    return _read_cartridge(dets, context, screenshot, cfg, bounds, label)[:3]


def _merge_cartridge_observation(saved, fresh):
    """Merge compatible observations, preserving conflicts and earlier good reads."""
    if saved.get("cart_conflict"):
        return
    for field in ("current_price", "peak_price", "current_rate", "peak_rate"):
        if saved.get(field) is not None and fresh.get(field) is not None and saved[field] != fresh[field]:
            saved.update(price_read_basis="observation_conflict", is_max_price=False,
                         max_price_basis="unconfirmed_amount")
            return
    if saved.get("price_read_basis") != "observation_conflict":
        for field in ("base_price", "current_price", "peak_price", "current_rate", "peak_rate"):
            if saved.get(field) is None and fresh.get(field) is not None:
                saved[field] = fresh[field]
        if fresh.get("price_read_basis") in ("initial_consistent", "local_rescue", "merged_consistent"):
            if not _price_issues(saved):
                saved.update(price_read_basis="merged_consistent", price_evidence=fresh.get("price_evidence", {}))
                _confirm_prices(saved)
    def signature(item):
        raw = item.get("target_cartridge", "")
        kind, complete = _classify_cart_type(raw)
        number = _tail_num(raw)
        return (kind, number) if kind and complete and number else None
    old, new = signature(saved), signature(fresh)
    if not new:
        return
    if old and old != new:
        saved.update(target_cartridge="", current_cartridge="", alt_cartridge="",
                     cart_conflict=True, cartridge_read_basis="observation_conflict")
        return
    if old:
        return
    for field in ("target_cartridge", "current_cartridge", "current_cartridge_raw", "cart_score",
                  "cartridge_read_basis", "cartridge_evidence"):
        if field in fresh:
            saved[field] = fresh[field]


def _action_params(argv) -> dict:
    """解析动作参数；显式写了非法 mode 时绝不回落成真实出售。"""
    raw = getattr(argv, "custom_action_param", None)
    if not raw:
        return {"mode": _MODE_SELL}
    params = raw if isinstance(raw, dict) else json.loads(str(raw))
    if not isinstance(params, dict):
        raise ValueError("custom_action_param 必须是对象")
    mode = params.get("mode", _MODE_SELL)
    if mode not in _VALID_MODES:
        raise ValueError(f"mode={mode!r} 非法，可选 {sorted(_VALID_MODES)}")
    if mode == _MODE_SELL and params.get("sale_scope", "recipes") not in ("recipes", "materials"):
        raise ValueError("sale_scope必须为recipes或materials")
    params["mode"] = mode
    return params


def _load_recipe_names() -> set[str]:
    """加载料理类别名录；失败时返回空集，预览模式宁可不卖也不猜类别。"""
    global _RECIPE_NAMES
    if _RECIPE_NAMES is not None:
        return _RECIPE_NAMES
    try:
        with open(_ITEM_DATA_FILE, "r", encoding="utf-8") as handle:
            records = json.load(handle)
        _RECIPE_NAMES = {
            canon(str(item.get("cn", "")).strip())
            for item in records
            if isinstance(item, dict) and item.get("category") == "Recipe" and item.get("cn")
        }
        mfaalog.info(f"[Arbitrage] 📖 料理类别名录加载 {len(_RECIPE_NAMES)} 项")
    except Exception as exc:
        _RECIPE_NAMES = set()
        mfaalog.error(f"[Arbitrage] ❌ 料理类别名录加载失败({exc})，预览模式将不执行出售")
    return _RECIPE_NAMES

def _new_scan(target_rate_floor: int | None = None) -> dict:
    """创建带明确覆盖语义的价目表观察。"""
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "items": [],
        "pages_scanned": 0,
        # complete 表示本次声明的扫描范围完成；是否扫完整表看 full_list_complete。
        "complete": False,
        "sale_candidates_complete": False,
        "full_list_complete": False,
        "termination_reason": "stopped",
        "target_rate_floor": target_rate_floor,
        "lowest_observed_rate": None,
        "rate_order_safe": True if target_rate_floor is not None else None,
    }


def _possession_scan_plan(snapshot: dict | None, recipe_names: set[str]) -> dict:
    """从完整公共行情生成已有物扫描计划；证据不足时要求全量扫描。"""
    unavailable = {
        "usable": False,
        "skip": False,
        "target_rate_floor": None,
        "target_names": [],
    }
    if not snapshot or not snapshot.get("complete") or not recipe_names:
        return unavailable

    targets = []
    for item in snapshot.get("items") or []:
        if not isinstance(item, dict) or not item.get("is_max_price"):
            continue
        name = canon(str(item.get("name") or "").strip())
        if name not in recipe_names:
            continue
        rate = item.get("current_rate")
        targets.append((name, rate))

    if not targets:
        return {
            "usable": True,
            "skip": True,
            "target_rate_floor": None,
            "target_names": [],
        }
    if any(not isinstance(rate, int) or isinstance(rate, bool) for _, rate in targets):
        return unavailable

    return {
        "usable": True,
        "skip": False,
        "target_rate_floor": min(rate for _, rate in targets),
        "target_names": [name for name, _ in targets],
    }



@AgentServer.custom_action("ArbitrageSellController")
class ArbitrageSellController(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _action_params(argv)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            mfaalog.error(f"[Arbitrage] ❌ 出售主控参数非法({exc})，为避免误卖已中止")
            return False

        mode = params["mode"]
        mfaalog.info(f"[Arbitrage] 🚀 商店套利-出售主控器启动(mode={mode})")
        if not sync_from_context(context, where=f"ArbitrageSellController/{mode}"):
            return False
        selling = mode != _MODE_PREVIEW_ALL
        if selling and not context.set_anchor("Replenish_ShopEntry", "Arbitrage_Merchant_NoDiscount_Entry"):
            mfaalog.error("[Arbitrage] 出售进店入口设置失败")
            return False
        completed = self._run(context, params)
        # 整轮出售正常收尾才清锚；公共行情、子流程、失败及停止均保留选路。
        if completed and selling and not context.tasker.stopping:
            if not context.set_anchor("Replenish_ShopEntry", ""):
                mfaalog.error("[Arbitrage] 出售结束后清理进店入口失败")
                return False
        return completed

    def _run(self, context: Context, params: dict) -> bool:
        mode = params["mode"]
        # 尾号救援可调参:JSON attach 覆盖 py 默认(缺则用默认)。每轮取副本,不写默认表。
        self._rescue_cfg = _load_rescue_cfg(context)
        self._price_cfg = _load_price_cfg(context)

        # 翻页上限:业务可传,但不允许缺省成"无限"。
        try:
            max_scan_pages = max(1, int(params.get("max_scan_pages", _MAX_SCAN_PAGES_DEFAULT)))
        except (ValueError, TypeError):
            mfaalog.error("[Arbitrage] ❌ max_scan_pages 必须是正整数，为避免无界翻页已中止")
            return False

        if mode == _MODE_PREVIEW_ALL:
            try:
                cached = get_market_snapshot()
            except Exception as exc:
                cached = None
                mfaalog.error(f"[Arbitrage] ⚠️ 共享行情缓存读取失败({exc})，本轮重新扫描")
            if cached is not None:
                names = [item["name"] for item in cached.get("items", []) if item.get("is_max_price")]
                mfaalog.info(
                    f"[Arbitrage] ♻️ 已复用今日共享行情快照({len(names)}项)，跳过重复全量观察："
                    f"{', '.join(names) if names else '无'}"
                )
                return True

        preview_recipe_names = None
        possession_plan = None
        final_reserves = {}
        final_market = None
        final_market_day = None
        if mode == _MODE_SELL:
            if params.get("sale_scope", "recipes") == "recipes":
                final_reserves = {name: 0 for name in _load_recipe_names()}
            else:
                try:
                    policy = read_material_reserve_policy(context)
                    final_reserves = {name: policy.reserve_for(name) for name in policy.sale_eligible
                                      if name not in _load_recipe_names() and policy.reserve_for(name) >= 0}
                    mfaalog.info(f"[⑤材料] 保留模式={policy.mode}；仅峰值出售超过保留量的部分")
                except Exception as exc:
                    mfaalog.error(f"[⑤材料] 保留配置不可用({exc})，停止材料出售")
                    return False
            if not final_reserves:
                mfaalog.info("[Arbitrage] 当前没有获准出售的料理或材料")
                return True
            try:
                final_market_day = market_day()
                final_market = get_market_snapshot(final_market_day)
            except Exception as exc:
                mfaalog.warning(f"[Arbitrage] 无法读取今日完整行情({exc})")
            if not final_market or not final_market.get("complete"):
                mfaalog.error("[Arbitrage] 今日完整行情存档缺失，最终出售停止；请检查共享行情准备步骤")
                return False
        if mode == _MODE_PREVIEW_POSSESS:
            # ①有独立开关；完整料理目录与④共用，均不依赖③制作选择。
            try:
                sell_node = context.get_node_data("Arbitrage_PreSell_Entry")
                if not isinstance(sell_node, dict):
                    raise ValueError("开局出售节点不可读")
            except Exception as exc:
                mfaalog.error(f"[Arbitrage] 无法确认出售开关({exc})，跳过开局变现")
                return False
            if not sell_node.get("enabled", True) or sell_node.get("max_hit") == 0:
                mfaalog.info("[Arbitrage] ①已关闭，跳过开局变现")
                return True
            preview_recipe_names = _load_recipe_names()
            try:
                possession_market = get_market_snapshot()
            except Exception as exc:
                possession_market = None
                mfaalog.warning(f"[Arbitrage] ⚠️ 无法读取公共行情以计算已有物边界({exc})，改为全量扫描")
            possession_plan = _possession_scan_plan(possession_market, preview_recipe_names)
            if possession_plan["usable"] and possession_plan["skip"]:
                mfaalog.info("[Arbitrage] 💤 今日公共行情没有峰值料理，已有物列表无需扫描")
            elif possession_plan["usable"]:
                target_names = "、".join(possession_plan["target_names"])
                mfaalog.info(
                    f"[Arbitrage] 🎯 已有物动态边界={possession_plan['target_rate_floor']}%，"
                    f"覆盖今日峰值料理：{target_names}"
                )
            else:
                mfaalog.warning(
                    "[Arbitrage] ⚠️ 公共行情或料理峰值倍率不足以证明安全边界，已有物改为全量扫描"
                )

        if mode == _MODE_SELL:
            # 全量行情只决定卖什么、去哪卖；库存和选量在商品子窗口重新读取。
            scan = final_market
        elif possession_plan and possession_plan["usable"] and possession_plan["skip"]:
            scan = _new_scan()
            scan.update({
                "complete": True,
                "sale_candidates_complete": True,
                "termination_reason": "no_peak_recipe" if mode == _MODE_PREVIEW_POSSESS else "no_peak_target",
                "rate_order_safe": True,
            })
        else:
            rate_floor = (
                possession_plan["target_rate_floor"]
                if possession_plan and possession_plan["usable"]
                else None
            )
            scan = self._scan_price_list(
                context, max_scan_pages, stop_at_non_max=False,
                stop_below_rate=rate_floor,
            )
        peak_items = [item for item in scan["items"] if item.get("is_max_price")]
        peak_names = [item["name"] for item in peak_items]
        scope_label = ("今日完整行情存档" if mode == _MODE_SELL else
                       "全量价目表" if mode == _MODE_PREVIEW_ALL else "已有物品价目表")
        mfaalog.info(
            f"[Arbitrage] 📈 {scope_label}·今日峰值商品总览: "
            f"{', '.join(peak_names) if peak_names else '无'}"
        )

        if mode == _MODE_PREVIEW_ALL:
            try:
                stored = save_market_snapshot(scan)
            except Exception as exc:
                stored = False
                mfaalog.error(f"[Arbitrage] ❌ 共享行情快照写入异常({exc})")
            if stored:
                state = "完整" if scan["complete"] else "不完整"
                mfaalog.info(f"[Arbitrage] 💾 今日共享行情{state}观察已保存")
            return True

        if mode == _MODE_PREVIEW_POSSESS:
            try:
                stored = save_possession_snapshot(scan)
            except Exception as exc:
                stored = False
                mfaalog.error(f"[Arbitrage] ❌ 当前存档持有物观察写入异常({exc})")
            if stored:
                mfaalog.info(
                    f"[Arbitrage] 💾 当前存档可售持有物观察已保存"
                    f"({len(scan['items'])}项，其中峰值{len(peak_items)}项，数量未知，"
                    f"覆盖={'完整' if scan['complete'] else '未完成'})"
                )
            if context.tasker.stopping:
                mfaalog.info("[Arbitrage] 扫描已停止，已观察结果保留")
                return True
            whitelist_set = preview_recipe_names if preview_recipe_names is not None else _load_recipe_names()
            mfaalog.info("[Arbitrage] 🍳 启动变现只允许料理类别，材料与其他物品一律不卖")
        else:
            whitelist_set = set(final_reserves)
            mfaalog.info(f"[Arbitrage] 📋 期望售卖清单 ({len(whitelist_set)}项): {', '.join(whitelist_set)}")

        # ①④使用完整料理目录；⑤单独使用材料资格与保留量。
        recipe_names = preview_recipe_names if preview_recipe_names is not None else _load_recipe_names()
        reserves = final_reserves if mode == _MODE_SELL else {name: 0 for name in whitelist_set & recipe_names}
        excluded = whitelist_set - reserves.keys()
        if excluded:
            mfaalog.info(f"[Arbitrage] 未取得本次出售许可或需无限保留，跳过: {', '.join(sorted(excluded))}")

        targets_to_sell = [
            {
                "name": item["name"],
                "cartridge_raw": item.get("target_cartridge", ""),
                "cart_score": item.get("cart_score", 0.0),
                "cart_conflict": item.get("cart_conflict", False),
                "cartridge_alt": item.get("alt_cartridge", ""),
                "current_rate": item.get("current_rate"),
                "cartridge_read_basis": item.get("cartridge_read_basis", "unknown"),
                "reserve": reserves.get(canon(item["name"])),
            }
            for item in peak_items
            if canon(item["name"]) in reserves and item.get("name_confirmed", True)
            and item.get("price_read_basis") not in ("unconfirmed_price", "observation_conflict")
        ]

        # ==========================================
        # 3. 派发阶段：循环注入并执行售卖节点链
        # ==========================================
        if not targets_to_sell:
            if not scan.get("prices_complete", True) or not scan.get("names_complete", True):
                mfaalog.warning("[Arbitrage] 仍有未确认商品或金额，不能认定今日没有峰值目标")
                return False
            mfaalog.info("[Arbitrage] 💤 今日无符合条件的最高价商品，收工！")
            return True

        # 🌟 优化日志 3：列出最终交集的执行清单
        final_sell_names = [t["name"] for t in targets_to_sell]
        mfaalog.info(f"[Arbitrage] 🛒 确认共 {len(targets_to_sell)} 项物品待出售: {', '.join(final_sell_names)}")

        sold_ok, sold_fail, sold_skipped = [], [], []
        for idx, target in enumerate(targets_to_sell, 1):
            if context.tasker.stopping: break
            if mode == _MODE_SELL and market_day() != final_market_day:
                mfaalog.error("[Arbitrage] 游戏日已刷新，停止使用旧日行情派发出售")
                return False

            item_name = target["name"]
            cart_raw = target["cartridge_raw"]
            mfaalog.info(f"[Arbitrage] 👉 正在执行 {idx}/{len(targets_to_sell)}: 前往 [{cart_raw}] 售卖 [{item_name}]")

            # 类型缺失、编号分歧等均不能派发；保留解析原因，避免统一误报成尾号救援失败。
            if not _tail_num(cart_raw):
                mfaalog.warning(
                    f"[Arbitrage] 🚨 [{item_name}] 当前卡带未确认"
                    f"(原因={target['cartridge_read_basis']}，读数=[{cart_raw}]),"
                    f"跳过本项以免进错柜台空跑"
                )
                sold_fail.append(item_name)
                continue

            # 卡带识别质量轻告警(#B):低置信或上下分歧只提示,不阻断——读错最坏进错柜台当没卖掉,
            # 真相由下面的金币验证承担。商品 expected 由共用派发函数展开简繁名称。
            if target.get("cart_score", 1.0) < SCORE_MIN or target.get("cart_conflict"):
                mfaalog.warning(
                    f"[Arbitrage]   ⚠️ 卡带识别可疑(组分{target.get('cart_score', 0):.2f}"
                    f"{'·上下分歧' if target.get('cart_conflict') else ''})，"
                    f"将在子页核对名称、价格和本批数量"
                )

            # 候选序列(2026-08-03):首选=判读胜者;上下分歧且另一串带号时,金币验证失败后改试
            # 另一串一次(实录:双1.00分歧,首选"当前行17"进错柜台,真身在"每月行12")。
            cands = [cart_raw]
            alt_raw = target.get("cartridge_alt", "")
            # 去重比「匹配式」而非 OCR 原文(2026-08-03):原文差一个 帶/带 会被 _cart_expected 折成
            # 同一条正则(见上方 _CART_FUZZ;同趟 OCR 繁简混读是实录常态),按原文比会把一个生成完全
            # 相同 override 的候选塞进来,白跑一整轮 UI 往返且不可能有不同结果。
            # 只有匹配式真不同(=真会去到别的柜台)才值得回退重试。
            if (target.get("cart_conflict") and alt_raw and _tail_num(alt_raw)
                    and _cart_expected(alt_raw) != _cart_expected(cart_raw)):
                cands.append(alt_raw)

            outcome = None
            for cand in cands:
                if context.tasker.stopping:
                    break
                override_cfg = _sell_item_override(context, item_name)
                override_cfg["Arbitrage_Sell_PackShopSwich"] = {"expected": _cart_expected(cand)}
                current_rate = target.get("current_rate")
                if type(current_rate) is not int or not 0 < current_rate < 1000:
                    mfaalog.warning(f"[Arbitrage] [{item_name}] 本轮溢价率未知，跳过本项")
                    break
                override_cfg["Arbitrage_Sell_Item_Price_MaxCheck"] = {"expected": f"{current_rate}%"}
                try:
                    outcome = execute_sale_item(context, item_name, override_cfg, reserve=target["reserve"])
                except Exception as exc:
                    mfaalog.error(f"[Arbitrage] [{item_name}] 出售主控异常：{exc}")
                    return False
                if not outcome["page_ok"]:
                    mfaalog.error("[Arbitrage] 出售页面或账号未能确认恢复，停止后续派发")
                    return False
                # 只有未交易且明确找不到目标时才允许换候选柜台；详情失败不重放。
                if outcome["status"] != "skipped" or outcome["actual_quantity"]:
                    break
            if outcome and outcome["actual_quantity"]:
                mfaalog.info(f"[Arbitrage] [{item_name}] 已确认卖出{outcome['actual_quantity']}个")
            if outcome and outcome["status"] == "confirmed":
                sold_ok.append(item_name)
            elif outcome and outcome["status"] == "skipped":
                sold_skipped.append(item_name)
                mfaalog.info(f"[Arbitrage] [{item_name}] 当前无可卖数量")
            else:
                sold_fail.append(item_name)
                mfaalog.warning(f"[Arbitrage] [{item_name}] 未全部确认售出，停止该物品；"
                                "已确认数量保留，未知成交不会重复派发")

        if sold_fail:
            mfaalog.warning(
                f"[Arbitrage] ⚠️ 本轮 {len(sold_fail)}/{len(targets_to_sell)} 项未完成出售："
                f"{', '.join(sold_fail)}"
            )
        if sold_ok:
            mfaalog.info(f"[Arbitrage] 🎉 本轮实际售出 {len(sold_ok)} 项：{', '.join(sold_ok)}")
        elif sold_fail:
            mfaalog.warning("[Arbitrage] 本轮没有完整完成的物品，已确认成交量保留，详见逐项回报。")
        elif sold_skipped:
            mfaalog.info(f"[Arbitrage] 本轮{len(sold_skipped)}项无可卖数量")
        else:
            mfaalog.info(f"[Arbitrage] ➖ 本轮 {len(targets_to_sell)} 项待售,一项都未及处理(多半是收到停止指令)。")
        return True

    def _scan_price_list(self, context: Context, max_scan_pages: int, stop_at_non_max: bool,
                         stop_below_rate: int | None = None) -> dict:
        """扫描价目表；已有物可在公共行情推导出的安全倍率边界处提前结束。"""
        scan = _new_scan(stop_below_rate)
        seen_items = {}
        prev_page_key = None
        page_count = 1
        boundary_safe = stop_below_rate is not None
        last_new_rate = None

        def disable_rate_boundary(reason: str) -> None:
            nonlocal boundary_safe
            if not boundary_safe:
                return
            boundary_safe = False
            scan["rate_order_safe"] = False
            mfaalog.warning(
                f"[Arbitrage] ⚠️ 已有物倍率顺序证据不完整({reason})，取消提前终止并改为扫到底"
            )

        while not context.tasker.stopping:
            # 前两层(动态边界/内容指纹)失灵会无限翻页，硬上界把代价收敛成一次不完整观察。
            if page_count > max_scan_pages:
                scan["termination_reason"] = "max_pages"
                mfaalog.warning(
                    f"[Arbitrage] ⚠️ 已连续扫描 {max_scan_pages} 页仍未触及利润边界或页底，"
                    f"达到翻页上限强制结束。若价目表确实更长，请调大 max_scan_pages。"
                )
                break

            mfaalog.info(f"[Arbitrage] 📷 正在扫描第 {page_count} 页价目表...")
            try:
                page_results = self._parse_current_page(context)
            except Exception as exc:
                scan["termination_reason"] = "parse_exception"
                mfaalog.error(f"[Arbitrage] ❌ 第 {page_count} 页解析异常({exc})，结束扫描")
                break
            scan["pages_scanned"] = page_count
            if not page_results:
                scan["termination_reason"] = "parse_empty"
                mfaalog.warning("[Arbitrage] ⚠️ 识别失败或页面无商品，结束扫描。")
                break

            page_key = frozenset(canon(item["name"]) for item in page_results)
            # The last repeated page may contain the first usable read of an item.
            for item in page_results:
                previous = seen_items.get(canon(item["name"]))
                if previous is not None:
                    _merge_cartridge_observation(previous, item)
            if prev_page_key is not None and page_key == prev_page_key:
                scan["complete"] = True
                scan["sale_candidates_complete"] = True
                scan["full_list_complete"] = True
                if stop_below_rate is not None:
                    scan["rate_order_safe"] = boundary_safe
                scan["termination_reason"] = "repeated_page"
                mfaalog.info("[Arbitrage] 🛑 本页与上一页内容一致，判定价目表已到底，结束扫描。")
                break
            prev_page_key = page_key

            page_rates = [item.get("current_rate") for item in page_results]
            if stop_below_rate is not None and boundary_safe:
                if any(not isinstance(rate, int) or isinstance(rate, bool) for rate in page_rates):
                    disable_rate_boundary(f"第{page_count}页存在未读出的当前倍率")
                elif any(left < right for left, right in zip(page_rates, page_rates[1:])):
                    disable_rate_boundary(f"第{page_count}页倍率不是从高到低")

            reached_boundary = False
            for item in page_results:
                name = item["name"]
                if stop_at_non_max and not item["is_max_price"]:
                    reached_boundary = True
                    scan["complete"] = True
                    scan["sale_candidates_complete"] = True
                    scan["termination_reason"] = "non_max_boundary"
                    mfaalog.info(f"[Arbitrage] 🛑 扫描到非最高价商品 [{name}]，已触及利润边界，停止向下扫描。")
                    break

                rate = item.get("current_rate")
                if isinstance(rate, int) and not isinstance(rate, bool):
                    lowest = scan["lowest_observed_rate"]
                    scan["lowest_observed_rate"] = rate if lowest is None else min(lowest, rate)
                item_key = canon(name)
                if item_key not in seen_items:
                    if stop_below_rate is not None and boundary_safe:
                        if last_new_rate is not None and rate > last_new_rate:
                            disable_rate_boundary(
                                f"跨页新增商品 [{name}] 的 {rate}% 高于上一新增商品 {last_new_rate}%"
                            )
                        else:
                            last_new_rate = rate
                    seen_items[item_key] = item
                    scan["items"].append(item)

            if reached_boundary:
                break

            crossed_rate_boundary = (
                stop_below_rate is not None
                and boundary_safe
                and any(rate < stop_below_rate for rate in page_rates)
            )
            if crossed_rate_boundary:
                scan["complete"] = True
                scan["sale_candidates_complete"] = True
                scan["termination_reason"] = "target_rate_boundary"
                scan["rate_order_safe"] = True
                mfaalog.info(
                    f"[Arbitrage] 🛑 本页已从高到低越过 {stop_below_rate}% 动态边界，"
                    "今日峰值料理候选已全部覆盖"
                )
                break

            mfaalog.info("[Arbitrage] ⏬ 下滑翻页...")
            # CustomAction callbacks need a recognition result in MaaFw 5.12.2.
            # The wrapper uses an empty on_error so actual failures stay failed.
            swip_detail = context.run_task("Agt_PriceList_Swip")
            if swip_detail is None:
                scan["termination_reason"] = "swipe_not_started"
                mfaalog.warning("[Arbitrage] ⚠️ 翻页任务未能启动（节点缺失或正在停止），停止扫描。")
                break
            if not swip_detail.status.succeeded:
                scan["termination_reason"] = "swipe_failed"
                mfaalog.warning("[Arbitrage] ⚠️ 翻页任务执行失败，停止扫描。")
                break
            page_count += 1

        missing = [item["name"] for item in scan["items"]
                   if not _tail_num(item.get("target_cartridge", "")) or item.get("cart_conflict")]
        scan["cartridges_complete"] = not missing
        scan["unconfirmed_cartridges"] = missing
        for quality, field, bad in (("names", "name_confirmed", lambda value: value is False),
                                    ("prices", "price_read_basis", lambda value: value in
                                     ("unconfirmed_price", "observation_conflict"))):
            unresolved = [item["name"] for item in scan["items"] if bad(item.get(field))]
            scan[quality + "_complete"] = not unresolved
            scan["unconfirmed_" + quality] = unresolved
            if unresolved:
                label = "商品名" if quality == "names" else "金额"
                mfaalog.warning(f"[Arbitrage] 价目表{label}未确认{len(unresolved)}项：{', '.join(unresolved)}")
        if missing:
            mfaalog.warning(f"[Arbitrage] 价目表覆盖完成={scan['complete']}，卡带未确认{len(missing)}项：{', '.join(missing)}")
        return scan

    # ==========================================
    # 附：V8 图像解析引擎
    # ==========================================
    def _parse_current_page(self, context: Context) -> list:
        # 每商品占价目表两行:上子行(当前,与名同高)/下子行(每月最高价日)
        screenshot = context.tasker.controller.post_screencap().wait().get()
        # 防御:截图失败则安全退出当前页解析
        if screenshot is None:
            print("[Arbitrage] ❌ 严重错误: 底层截图获取失败 (返回 None)！跳过当前页解析。")
            return []

        # run 起始已按本轮 attach 取好副本；单测/直调本方法时回落只读默认表。
        rescue_cfg = getattr(self, "_rescue_cfg", None) or dict(_RESCUE_CFG)
        price_cfg = getattr(self, "_price_cfg", None) or _load_price_cfg(context)

        def _col(node):
            """跑某列窄 roi OCR,取 filtered → [{text,cx,cy}, ...](窄 roi 已圈好列,无需 cx 过滤分列)。"""
            reco = context.run_recognition(node, screenshot)
            out = []
            for r in (getattr(reco, "filtered_results", None) or []):
                x, y, w, h = r.box
                out.append({"text": r.text, "cx": x + w / 2, "cy": y + h / 2,
                            "x": x, "y": y, "w": w, "h": h,
                            "score": getattr(r, "score", 1.0)})
            return out

        names = _col(_COL_NAME)
        amounts = _col(_COL_AMOUNT)
        prices = _col(_COL_PRICE)
        carts = _col(_COL_CART)
        cart_roi = list(context.get_node_object(_COL_CART).recognition.param.roi)
        price_columns = {key: list(context.get_node_object(node).recognition.param.roi)
                         for key, node in (("name", _COL_NAME), ("amount", _COL_AMOUNT), ("rate", _COL_PRICE))}

        # 名锚:名列内非数字文本 = 各商品行(与上子行同高),按 y 升序、近距去重
        anchors = []
        for t in sorted(names, key=lambda t: t["cy"]):
            cleaned = re.sub(r'[^\w一-龥]', '', t["text"])
            if cleaned and not cleaned.isdigit():
                if not any(abs(t["cy"] - a["cy"]) < 30 for a in anchors):
                    identified = _read_item_name(context, screenshot, {**t, "text": cleaned})
                    anchors.append({"name": identified["name"], "cy": t["cy"], "identity": identified})
        if not anchors:
            return []
        # 商品行距中位数:供末行下子行搜索上界(无下一名锚时的兜底跨度)
        gaps = [anchors[i + 1]["cy"] - anchors[i]["cy"] for i in range(len(anchors) - 1)]
        gap_bound = sorted(gaps)[len(gaps) // 2] if gaps else 4 * SUBROW_TOL

        def _row_pcts(center_y):
            """价列内、与 center_y 同高(±SUBROW_TOL)的所有溢价率数字集合。"""
            out = set()
            for t in prices:
                if abs(t["cy"] - center_y) <= SUBROW_TOL:
                    m = RE_PCT.search(t["text"])
                    if m:
                        out.add(m.group(1))
            return out

        def _row_money(center_y):
            """金额列内的整数集合；兼容金额与百分比粘成 `4.416120%`。"""
            out = set()
            for item in amounts:
                if abs(item["cy"] - center_y) > SUBROW_TOL:
                    continue
                value = _money_token_value(item["text"])
                if value is not None:
                    out.add(value)
            return out

        results = []
        previous_monthly_y = None
        for i, row in enumerate(anchors):
            item_data = {
                "name": row["name"],
                "raw_name": row["identity"]["raw"],
                "name_confirmed": row["identity"]["confirmed"],
                "name_evidence": row["identity"],
                "is_max_price": False,
                "max_price_basis": "unreadable",
                "current_price": None,
                "peak_price": None,
                "current_rate": None,
                "peak_rate": None,
                "target_cartridge": "",
                "cart_score": 0.0,
                "cart_conflict": False,
                "alt_cartridge": "",
            }
            ny = row["cy"]                                    # 上子行(当前)y = 名锚 y
            next_ny = anchors[i + 1]["cy"] if i + 1 < len(anchors) else ny + gap_bound

            # 下子行(每月)y:本商品带内(next_ny 为界,防吸入下一商品上子行)、名字下方最近的价格行
            below_ys = sorted(
                t["cy"] for t in prices
                if ny + SUBROW_TOL < t["cy"] < next_ny and RE_PCT.search(t["text"])
            )
            mon_y = below_ys[0] if below_ys else None
            if mon_y is None:
                # 倍率漏读时仍可由金额行定位；不借用月度卡带自身来猜当前编号。
                money_ys = sorted(t["cy"] for t in amounts if ny + SUBROW_TOL < t["cy"] < next_ny
                                  and _money_token_value(t["text"]) is not None)
                mon_y = money_ys[0] if money_ys else None

            # 金额相等才是最终满价判据。低价物品会因向下取整在 117%/118% 提前撞到峰值金额；
            # 只比溢价率会漏卖。金额列有一侧读不到时才退回旧的溢价率交集判据。
            top_money = _row_money(ny)
            bot_money = _row_money(mon_y) if mon_y is not None else set()
            top_pct = _row_pcts(ny)
            bot_pct = _row_pcts(mon_y) if mon_y is not None else set()
            if len(top_money) == 1:
                item_data["current_price"] = next(iter(top_money))
            if len(bot_money) == 1:
                item_data["peak_price"] = next(iter(bot_money))
            if len(top_pct) == 1:
                item_data["current_rate"] = int(next(iter(top_pct)))
            if len(bot_pct) == 1:
                item_data["peak_rate"] = int(next(iter(bot_pct)))
            current_dets = [d for d in amounts if abs(d["cy"] - ny) <= SUBROW_TOL]
            monthly_dets = [d for d in amounts if mon_y is not None and abs(d["cy"] - mon_y) <= SUBROW_TOL]
            bases = [d for d in names if ny + SUBROW_TOL < d["cy"] < next_ny
                     and _money_token_value(d["text"]) is not None]
            current_centers = [d["cy"] for d in prices if abs(d["cy"] - ny) <= SUBROW_TOL
                               and RE_PCT.search(d["text"])]
            quote_y = sum(current_centers) / len(current_centers) if current_centers else ny
            _read_prices(context, screenshot, item_data, current=current_dets, monthly=monthly_dets,
                         bases=bases, centers=(quote_y, mon_y), columns=price_columns, cfg=price_cfg)

            split_y = _cart_split_y(names, amounts, ny, mon_y, next_ny)
            if len(anchors) == 1 and split_y is not None:
                # 单项画面没有相邻名称可量行距，改用实读上下行距补足商品范围。
                lower_y = mon_y if mon_y is not None else 2 * split_y - ny
                next_ny = max(next_ny, ny + 2 * (lower_y - ny))
            if split_y is None:
                day_dets, month_dets, bounds = [], [], None
            else:
                day_dets, month_dets, bounds = _cart_regions(
                    carts, ny, mon_y, next_ny, cart_roi, split_y=split_y,
                    previous_monthly_y=previous_monthly_y)
            previous_monthly_y = mon_y if mon_y is not None else (
                2 * split_y - ny if split_y is not None else None)
            day_cart, day_score, basis, evidence = _read_cartridge(
                day_dets, context, screenshot, rescue_cfg, bounds, row['name'])
            item_data["target_cartridge"] = day_cart
            item_data["current_cartridge"] = day_cart
            item_data["current_cartridge_raw"] = _cart_group(day_dets)[0]
            item_data["monthly_cartridge"] = _cart_group(month_dets)[0]
            item_data["cartridge_read_basis"] = basis
            item_data["cartridge_evidence"] = {key: value for key, value in evidence.items() if key != "attempts"}
            item_data["cart_score"] = day_score

            results.append(item_data)

        return results
