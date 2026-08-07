import json
import re
from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer
from utils import mfaalog
from utils.name_i18n import canon
from action import gold_verify

# ==========================================
# 三列各自窄 roi OCR(#Q2.5,2026-07-24)：名/价/卡带在各自节点的 roi 内分别识别。
# 窄 roi 让小字(卡带尾号)可靠——大 roi 整表 OCR 会漏检小数字(07-24实录:活動卡的号
# 在整表 ReadList 里检不出,单独窄 roi 一放大就认出)。roi 由各节点 JSON 承载,
# run_recognition 自动生效,故不再需要 get_node_data 读列带、也无需 cx 过滤分列。
# ==========================================
_COL_NAME = "Arbitrage_Sell_Col_Name"
_COL_PRICE = "Arbitrage_Sell_Col_Price"
_COL_CART = "Arbitrage_Sell_Col_Cart"

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
SUBROW_TOL = 14      # 同子行 y 容差(子行间距约30px,商品行距约73px)
SCORE_MIN = 0.6      # 卡带选中组组分低于此=低置信,打WRN(实录错读曾得0.51,正确读数更高,#B)


# 卖出验证(2026-08-06 改)：金币不再由主控在派发前后自测,改由链内 A/B 两个动作节点测量,
# 主控只取结论 —— 时序契约见 gold_verify.py。主控那对读数跨越整条出售链,时间窗长且看不见
# 链条内部走到哪一步、卖了几件;链内测点贴着出售动作,还能覆盖连续出售的多件累计。
# run_task 的返回值靠不住这件事没变:它只表示链条"正常结束",选卡带找不到目标、SmartSwipe
# 判触底 JumpOut 时同样成功(07-22实录:桑格利亚酒一件没卖仍报成功),所以成败仍以金币为准。


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


# 卡带上下两子行交叉核对(#B,2026-07-24)：满价商品「当前档」与「每月最高档」是同一柜台,卡带名
# 理应同串。两子行各拼一组(类型+号两个det),组分=组内最小det置信(短板:类型和号都得对才能进对
# 柜台),取组分高的一组整串去匹配菜单。卡带只决定「去哪卖」,读错最坏是进错柜台、首页找不到物品名
# →当没卖掉(现有金币验证兜底WRN),绝不误卖,故不做类型闭集纠错/错字重映射(错字是开放集收不过来,
# 收益仅省一次空跑,不划算)。
def _cart_group(dets) -> tuple:
    """一子行的卡带 det 组 → (整串, 组分)。串按 cx 序拼接+清洗;组分取组内最小 det 置信。空组→("",0.0)。"""
    dets = sorted(dets, key=lambda t: t["cx"])
    if not dets:
        return "", 0.0
    raw = "".join(t["text"] for t in dets)
    text = re.sub(r'[^\w一-龥]', '', raw)
    return text, min(t["score"] for t in dets)


# ==========================================
# 尾号救援(2026-07-25)：DBNet 对孤立细「1」召回不稳——同页尾号里多位数(11/10/2)稳检、单个细「1」
# 漏检,换 4mb~60mb 多个 det 均无解:坏的是 det,rec 本身能读。故某子行拼组后无尾号时,以类型 det 框为
# 锚,其正下方 only_rec 跳过 det 直接把号读回。节点靠 override 内联,不占 JSON(同 RDDraw 回显)。
#
# roi 宽窄互补(21:33 实测复盘)：值恒右对齐于类型右缘,但噪声随 crop 边界而变——【宽 roi】给 rec 足
# 够上下文、多数行读对,但有的行被左侧整排类型字底带偏(→00/=—/e 前缀,如实录 0021/e21/=—1);【窄
# roi】只圈右侧值带、躲开左侧字底,但「1」正处「帶」正下方,窄了反被「帶」右下钩带偏(→」)。二者恰
# 好互补(同帧宽崩的行窄能读对、反之亦然)。故每行按【宽+窄】各读一次,取「置信最高且号合理(1~99 无
# 前导0)」的一版——对 crop 边界做小集成。全不合理/全低分则保持无号,交上层按「缺号可疑」跳过柜台。
# ==========================================
# 扫描翻页的硬上界。业务可用节点的 custom_action_param 传 max_scan_pages 覆盖,
# 但不允许无限翻 —— 见 run() 里三层终止条件的说明。
_MAX_SCAN_PAGES_DEFAULT = 30

_RESCUE_NODE = "Arbitrage_Sell_Cart_RescueNum"
# 救援可调参:全部无量纲(相对"实检类型 det 框"的比例)——尺度锚定 H=类高中位数、W=类型块宽、yb=类型下缘,
# 故字号/布局不同的两端(PC 繁体小字、ADB 简体大字)可共用同一份配置。可被 _RESCUE_NODE.attach 覆盖,缺项回落此默认。
# 【调参指南】改值一律落 _RESCUE_NODE.attach(JSON)、不动 py;先看落图 vision/*_Sell_Cart_RescueNum_*.jpg 的红框对症:
#   · 救援总失败(全档低分/不合理=号没框住):号被切顶/切底 → 增/移 y_shifts 档 或 调大 h_frac;
#     号被左侧类型字底带出前缀(0021/=—/e 之类) → 调小 narrow_frac(值带更窄、更靠右缘,躲开字底)。
#   · 救出怪值且被选中:调高 min_score(更严),或删掉最易蹭字底的 y_shifts 档(候选变少→误读面变小)。
#   · 换端/换分辨率:参数是"相对类型框"的比例,一般无需改;仅当号高占比或号横向位置占比本身变了,才分别动 h_frac / narrow_frac。
#   · 人工核对:每条救援日志都带名锚(商品名·子行),对照 vision/ 落图逐行核。
_RESCUE_CFG = {
    "min_score": 0.6,           # 号 rec 置信下限:集成里最优的合理号仍低于此=糊读,判救援失败
    "narrow_frac": 0.5,         # 窄 roi 宽 = narrow_frac*W(右对齐值带,避左侧类型字底)
    "pad_frac": 0.10,           # 宽 roi 横向外扩 = pad_frac*W(左右各;宽 = W+2*pad)
    "h_frac": 1.2,              # 号 roi 高 = h_frac*H
    "y_shifts": [-0.2, 0.0, 0.2],  # 带顶相对 yb 的纵向位移(单位=H);含下移档以躲开类型字底残笔
}


def _load_rescue_cfg(context) -> dict:
    """从 _RESCUE_NODE.attach 生成**本轮**救援参数副本(缺项/坏值各自回落 py 默认)。run 起始调一次。

    上面的 _RESCUE_CFG 是只读默认表,本函数绝不写它——早先的原地覆盖写法有两个坑:
      · 半覆盖:逐项转型时中途抛异常被 except 兜住,前面几项已经写进全局了,而日志却报
        "沿用内置默认",照着日志查会以为全是默认值;
      · 粘滞:PatchPipeline 能改 attach,任务结束框架撤销它自己那半边 override,但这个
        全局 dict 框架不知道(同 pipeline_manager 的 _LEDGERS 处境),上一轮的覆盖值会
        一直留着——下一轮 attach 里没这个 key 了,也回不到 py 默认。
    改为每轮取副本后,坏值只影响它自己那一项,默认表恒定。
    """
    cfg = dict(_RESCUE_CFG)
    try:
        node = context.get_node_object(_RESCUE_NODE)
        attach = getattr(node, "attach", None) if node else None
        if not attach:
            return cfg
        for k in _RESCUE_CFG:
            if k in attach:
                try:
                    cfg[k] = type(_RESCUE_CFG[k])(attach[k])
                except (TypeError, ValueError):
                    mfaalog.warning(
                        f"[Arbitrage] ⚠️ 救援可调参 {k}={attach[k]!r} 非法"
                        f"(应为 {type(_RESCUE_CFG[k]).__name__}),该项回落默认 {_RESCUE_CFG[k]!r}"
                    )
    except Exception as e:
        mfaalog.warning(f"[Arbitrage] ⚠️ 救援可调参读取异常({e}),整份沿用内置默认")
    return cfg


def _tail_num(s: str) -> str:
    """整串尾部连续数字(尾号);无则空串。"""
    m = re.search(r'(\d+)\s*$', s)
    return m.group(1) if m else ""


def _rescue_rois(type_dets: list, cfg: dict) -> list:
    """尾号救援候选 roi(绝对坐标)。尺度全锚定实检类型框:H=类高中位数(自适应端字号)、W=类型块宽、
    yb=类型下缘。横向宽/窄互补(宽=类型同宽+外扩取上下文;窄=右对齐值带避左侧字底);纵向按 ±%H 多位移
    (含下移档躲类型字底残笔)。候选 = {宽,窄} × y_shifts。cfg 由 _load_rescue_cfg 逐轮生成。"""
    left = min(d["x"] for d in type_dets)
    right = max(d["x"] + d["w"] for d in type_dets)
    yb = max(d["y"] + d["h"] for d in type_dets)
    hs = sorted(d["h"] for d in type_dets)
    H = hs[len(hs) // 2]                                    # 类高中位数 = 尺度单位
    W = max(1, right - left)
    pad = max(1, int(round(cfg["pad_frac"] * W)))
    nw = max(1, int(round(cfg["narrow_frac"] * W)))
    h = max(1, int(round(cfg["h_frac"] * H)))
    rois = []
    for s in cfg["y_shifts"]:
        top = max(0, int(round(yb + s * H)))
        rois.append([max(0, left - pad), top, W + 2 * pad, h])   # 宽
        rois.append([max(0, right - nw), top, nw + pad, h])       # 窄
    return rois


def _rescue_tail_num(context, screenshot, type_dets: list, cfg: dict) -> tuple:
    """多候选 roi 各 only_rec,收合理号(1~99无前导0);先按号串投票取多数(真号在多档复现、杂读难复现),
    同票再以最高 score 破平 → (号串, 该号最高 score)。全不中或最优 score<min_score → ("",0.0)。"""
    if not type_dets:
        return "", 0.0
    votes = {}   # num -> [票数, 最高 score]
    for roi in _rescue_rois(type_dets, cfg):
        roi = [int(v) for v in roi]
        try:
            reco = context.run_recognition(
                _RESCUE_NODE, screenshot,
                pipeline_override={_RESCUE_NODE: {"recognition": "OCR", "roi": roi, "only_rec": True}},
            )
        except Exception as e:
            mfaalog.warning(f"[Arbitrage] ⚠️ 尾号救援 OCR 异常({e})")
            continue
        cand = (getattr(reco, "filtered_results", None)
                or getattr(reco, "all_results", None) or [])
        if not cand:
            continue
        top = max(cand, key=lambda r: getattr(r, "score", 0.0))
        sc = getattr(top, "score", 0.0)
        num = re.sub(r'\D', '', getattr(top, "text", "") or "")
        if not re.fullmatch(r'[1-9]\d?', num):   # 只收合理号,挡 0021/」/=— 噪声
            continue
        v = votes.setdefault(num, [0, 0.0])
        v[0] += 1
        v[1] = max(v[1], sc)
    if not votes:
        return "", 0.0
    best_num = max(votes, key=lambda n: (votes[n][0], votes[n][1]))   # 票数优先,同票比 score
    best_sc = votes[best_num][1]
    if best_sc < cfg["min_score"]:
        return "", 0.0
    return best_num, best_sc


def _cart_group_rescued(dets, context, screenshot, cfg: dict, label="") -> tuple:
    """_cart_group 外加尾号救援:拼组后若无尾号,以类型 det 为锚 only_rec 补号(号计入组分,取短板)。
    置于置信率对比之前,故上/下两子行各自先补号再比对(#B 交叉核对拿到的是补齐后的整串)。
    label=行标识(商品名·子行),仅用于日志人工核对定位。"""
    dets = list(dets)
    text, score = _cart_group(dets)
    if text and not _tail_num(text):
        type_dets = [d for d in dets
                     if not re.sub(r'[^\w一-龥]', '', d["text"]).isdigit()]
        num, nsc = _rescue_tail_num(context, screenshot, type_dets, cfg)
        if num:
            text, score = text + num, min(score, nsc)
            mfaalog.info(f"[Arbitrage]   ↳ 尾号救援成功: {label} → {text}(号置信{nsc:.2f})")
        else:
            mfaalog.warning(f"[Arbitrage]   ⚠️ 尾号救援失败: {label},[{text}] 仍缺号,将按缺号可疑处置")
    return text, score


@AgentServer.custom_action("ArbitrageSellController")
class ArbitrageSellController(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        mfaalog.info("[Arbitrage] 🚀 商店套利-出售主控器启动")
        # 尾号救援可调参:JSON attach 覆盖 py 默认(缺则用默认)。每轮取副本,不写默认表。
        self._rescue_cfg = _load_rescue_cfg(context)

        # 翻页上限:业务可传,但不允许缺省成"无限"。
        max_scan_pages = _MAX_SCAN_PAGES_DEFAULT
        if argv.custom_action_param:
            try:
                _p = json.loads(argv.custom_action_param)
                if isinstance(_p, dict) and "max_scan_pages" in _p:
                    max_scan_pages = max(1, int(_p["max_scan_pages"]))
            except (ValueError, TypeError) as e:
                mfaalog.warning(
                    f"[Arbitrage] ⚠️ custom_action_param 解析失败({e})，翻页上限沿用默认 {max_scan_pages}"
                )

        # ==========================================
        # 1. 提取并合并 Attach 白名单
        # ==========================================
        whitelist_set = set()

        # 假设我们将此动作绑定在 Arbitrage_ShopSell_Active 节点
        node_obj = context.get_node_object("Arbitrage_ShopSell_Active")

        if node_obj and node_obj.attach:
            # 遍历 attach 中的所有 key (default, Drops, 以及 UI 传进来的 SellName)
            for _, val_str in node_obj.attach.items():
                if isinstance(val_str, str) and val_str.strip():
                    # 按照逗号、分号、中文逗号切分
                    raw_items = [x.strip() for x in re.split(r'[，,;|]+', val_str) if x.strip()]
                    for item in raw_items:
                        # 使用和 OCR 底层一模一样的清洗规则，保证 100% 绝对匹配
                        cleaned_item = re.sub(r'[^\w\u4e00-\u9fa5]', '', item)
                        if cleaned_item:
                            # 归一化到规范简体：白名单可简/繁书写，统一后与 OCR 名同域核对。
                            whitelist_set.add(canon(cleaned_item))

        if not whitelist_set:
            mfaalog.warning("[Arbitrage] ⚠️ 未读取到任何待售物品白名单，流程结束。")
            return True

        mfaalog.info(f"[Arbitrage] 📋 期望售卖清单 ({len(whitelist_set)}项): {', '.join(whitelist_set)}")

        # ==========================================
        # 2. 扫描阶段：识别当前页 -> 翻页 -> 截断
        # ==========================================
        targets_to_sell = [] # 记录所有达标待售的商品
        all_max_price_items = []   # 记录所有扫描到的最高价商品（仅用于展示）
        page_count = 1
        prev_page_key = None    # 上一页的内容指纹,用于识别"已滑到底"

        while not context.tasker.stopping:
            # 第 3 层终止:硬上界。
            # 前两层(利润边界截断 / 内容指纹)都是业务判据,可能因数据分布或 OCR 抖动失灵,
            # 而失灵方向是"永不终止",代价不可恢复;硬上界失灵方向是"提前结束",下次跑能补上。
            # 两者代价不对称,所以这道防线必须在,哪怕它几乎永远不触发。
            # 触发即说明前两层都没兜住,按异常记 warning,不静默。
            if page_count > max_scan_pages:
                mfaalog.warning(
                    f"[Arbitrage] ⚠️ 已连续扫描 {max_scan_pages} 页仍未触及利润边界或页底，"
                    f"达到翻页上限强制结束。若价目表确实更长，"
                    f"请用节点的 custom_action_param 调大 max_scan_pages。"
                )
                break

            mfaalog.info(f"[Arbitrage] 📷 正在扫描第 {page_count} 页价目表...")

            # 调用内部的 V8 图像解析引擎
            page_results = self._parse_current_page(context)
            if not page_results:
                mfaalog.warning("[Arbitrage] ⚠️ 识别失败或页面无商品，结束扫描。")
                break

            # 第 2 层终止:内容指纹。价目表滑到底后再滑不动,本页会与上一页完全相同,
            # 而 has_non_max 在"整页全是最高价"时不会触发,过去这里就是死循环的入口。
            # 用 canon 归一化后的名字集合而非 OCR 原文比较——原文里个别字的识别抖动
            # 会让指纹永不相等,这道防线就形同虚设了。
            page_key = frozenset(canon(it["name"]) for it in page_results)
            if prev_page_key is not None and page_key == prev_page_key:
                mfaalog.info("[Arbitrage] 🛑 本页与上一页内容一致，判定价目表已到底，结束扫描。")
                break
            prev_page_key = page_key

            has_non_max = False
            for item in page_results:
                name = item["name"]
                is_max = item["is_max_price"]
                cart = item["target_cartridge"]

                # 触发截断：遇到非最高价商品
                if not is_max:
                    has_non_max = True
                    mfaalog.info(f"[Arbitrage] 🛑 扫描到非最高价商品 [{name}]，已触及利润边界，停止向下扫描。")
                    break

                # 记录所有扫描到的最高价商品（去重保存）
                if name not in all_max_price_items:
                    all_max_price_items.append(name)

                # 检查是否在白名单中（仅归一化「比较用副本」；name 原文保留回填售卖链，
                # 繁体端须以 OCR 原文匹配同语言 UI，切勿把归一化后的简体名传给 expected）
                if canon(name) in whitelist_set:
                    # 查重防抖 (防止翻页重叠导致同个物品被记录两次)
                    if not any(t["name"] == name for t in targets_to_sell):
                        targets_to_sell.append({
                            "name": name,
                            "cartridge_raw": cart,
                            "cart_score": item.get("cart_score", 0.0),
                            "cart_conflict": item.get("cart_conflict", False),
                            "cartridge_alt": item.get("alt_cartridge", "")
                        })

            if has_non_max:
                break

            # 翻页动作：调用你写好的精准滑动链
            mfaalog.info("[Arbitrage] ⏬ 下滑翻页...")
            # run_task 返回 Optional[TaskDetail],返回对象只表示任务被成功提交,
            # 成败在 .status 里。旧写法 `if not swip_success` 把 TaskDetail 当 bool 用,
            # 而 Arbitrage_Swip_PriceList 是纯 Swipe 节点必然能起来 —— 那个分支从来没执行过,
            # 于是"滑不动"这件事对本循环完全不可见。
            swip_detail = context.run_task("Arbitrage_Swip_PriceList")
            if swip_detail is None:
                mfaalog.warning("[Arbitrage] ⚠️ 翻页任务未能启动（节点缺失或正在停止），停止扫描。")
                break
            if not swip_detail.status.succeeded:
                mfaalog.warning("[Arbitrage] ⚠️ 翻页任务执行失败，停止扫描。")
                break

            page_count += 1

        # 🌟 优化日志 2：列出今日市面上的所有最高价商品
        mfaalog.info(f"[Arbitrage] 📈 今日最高价&有库存商品总览: {', '.join(all_max_price_items) if all_max_price_items else '无'}")

        # ==========================================
        # 3. 派发阶段：循环注入并执行售卖节点链
        # ==========================================
        if not targets_to_sell:
            mfaalog.info("[Arbitrage] 💤 今日无符合条件的最高价商品，收工！")
            return True

        # 🌟 优化日志 3：列出最终交集的执行清单
        final_sell_names = [t["name"] for t in targets_to_sell]
        mfaalog.info(f"[Arbitrage] 🛒 扫描完毕！确认共 {len(targets_to_sell)} 项物品待出售: {', '.join(final_sell_names)}")

        sold_ok, sold_fail = [], []
        for idx, target in enumerate(targets_to_sell, 1):
            if context.tasker.stopping: break

            item_name = target["name"]
            cart_raw = target["cartridge_raw"]
            mfaalog.info(f"[Arbitrage] 👉 正在执行 {idx}/{len(targets_to_sell)}: 前往 [{cart_raw}] 售卖 [{item_name}]")

            # 缺尾号拦截(2026-07-25):尾号现实一定存在,拼组+救援后仍无号 = 识别彻底失手。按既定策略
            # 报警并跳过——绝不去「只有类型、没有号」的柜台臆测消歧(最坏进错柜台空跑),交下轮重扫。
            if not _tail_num(cart_raw):
                mfaalog.warning(
                    f"[Arbitrage] 🚨 [{item_name}] 卡带尾号缺失且救援失败([{cart_raw}]),"
                    f"跳过本项以免进错柜台空跑"
                )
                sold_fail.append(item_name)
                continue

            # 卡带识别质量轻告警(#B):低置信或上下分歧只提示,不阻断——读错最坏进错柜台当没卖掉,
            # 真相由下面的金币验证承担。派发链的 expected 沿用 OCR 原文(繁体端须同语言匹配菜单)。
            if target.get("cart_score", 1.0) < SCORE_MIN or target.get("cart_conflict"):
                mfaalog.warning(
                    f"[Arbitrage]   ⚠️ 卡带识别可疑(组分{target.get('cart_score', 0):.2f}"
                    f"{'·上下分歧' if target.get('cart_conflict') else ''})，"
                    f"若进错柜台将当作未卖出处理"
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

            verdict = None                     # 跨候选保留的最强结论(gold_verify 的 dict)
            # 初值必须是 None 而非 False:下方收尾分支按 `is not None` 判"链条跑过了",
            # False 会被判成跑过 → 取 .status 抛 AttributeError(停止指令恰好落在内层
            # 循环头、一轮未跑时可达)。
            sell_result = None
            for att, cand in enumerate(cands):
                # 停止指令只打断框架侧任务,管不到 Python 控制流(post_stop 清队列+断当前任务,
                # stopping 仅表示"已发指令未结束")。不在循环头拦一道,回退轮仍会白提一次注定被
                # 拒的 run_task。本文件的翻页循环、外层目标循环与 shop_buy_fav_controller 的
                # 重试/动作循环都是在循环头检查,此处补齐同一约定。
                if context.tasker.stopping:
                    break
                # 上轮链条没跑成(任务没提上去 / status 失败) = 出售流程自身垮了,换个卡带串
                # 解决不了,不空跑。判成败必须走 _task_ok:`not sell_result` 只拦得住"没提上去"。
                # 注意"链条正常收尾但没走到 B"时 _task_ok 仍为真,那种要继续试下一个候选。
                if att and not _task_ok(sell_result):
                    break
                if att:
                    mfaalog.warning(
                        f"[Arbitrage]   🔁 上下分歧回退：首选 [{cart_raw}] 未售出，改试 [{cand}]"
                    )
                # 核心：构造多节点参数替换字典
                # 卡带名走容错正则(OCR 把'剧'读成'则'等,前缀模糊卡号精确)
                cart_pat = _cart_expected(cand)
                if cart_pat != cand:
                    mfaalog.info(f"[Arbitrage]   ↳ 卡带匹配用容错式: {cart_pat}")
                override_cfg = {
                    "Arbitrage_Sell_PackShopSwich": {
                        "expected": cart_pat
                    },
                    "Arbitrage_Sell_Item_ListTraverse": {
                        "expected": item_name
                    }
                }

                # 发包前清槽:不清的话,本轮链条若没走到 B(中途断了),会读到上一轮的残留结论。
                gold_verify.clear_verdict()

                # 拉起 JSON 端的出售链，并阻塞等待它执行完毕
                # 起点设为进入出售菜单的识别节点
                sell_result = context.run_task("Arbitrage_Sell_HUB", pipeline_override=override_cfg)

                # 金币由链内 A/B 测量(见 gold_verify 的时序契约),这里只取结论。
                # None = B 压根没执行;delta is None = B 跑了但两端读数缺一个。
                this_round = gold_verify.take_verdict()
                if _verdict_rank(this_round) > _verdict_rank(verdict):
                    verdict = this_round
                delta = this_round.get("delta") if this_round else None
                if delta is not None and delta > 0:
                    break            # 已售出,毙掉后续候选,少跑一个柜台
                # delta == 0(确凿没卖出) 与 delta is None(无法判断) 都按"这个候选不行"处理,
                # 接着试下一个卡带候选 —— 但绝不拿同一套参数重跑,那只会复现同样的结果。

            delta = verdict.get("delta") if verdict else None
            before = verdict.get("before") if verdict else None
            after = verdict.get("after") if verdict else None

            if delta is not None and delta > 0:
                sold_ok.append(item_name)
                mfaalog.info(
                    f"[Arbitrage] ✅ [{item_name}] 确认售出，金币 +{delta:,} "
                    f"({before:,} → {after:,})"
                )
            elif delta is not None:
                sold_fail.append(item_name)
                # delta < 0 出售链里不该出现(卖东西只会加钱),真出现多半是读串了行或期间
                # 有别的消耗,单独点出来免得当成普通的"没卖出"放过去。
                extra = "金币不增反减，读数可疑" if delta < 0 else "金币无变化"
                mfaalog.warning(
                    f"[Arbitrage] ❌ [{item_name}] 未实际售出！{extra}"
                    f"({before:,} → {after:,})，"
                    f"链条多半卡在选卡带/物品定位，run_task 的成功是假信号"
                )
            else:
                # 无法判断一律计入失败(2026-08-06 定):金币是次要保险,保险失效时不能当成
                # "大概卖成了"放过 —— 物品存在性才是主判据,而它此刻同样没有结论。
                sold_fail.append(item_name)
                if not _task_ok(sell_result):
                    reason = "售卖流程未能启动（节点缺失或正在停止）" if sell_result is None else "售卖流程执行失败"
                elif verdict is None:
                    reason = "链条未走到金币复核点（B 未执行，多半中途断链）"
                else:
                    reason = f"金币读数不可用（前 {before} / 后 {after}）"
                mfaalog.warning(f"[Arbitrage] ⚠️ [{item_name}] 无法判断是否售出：{reason}")

        if sold_fail:
            mfaalog.warning(
                f"[Arbitrage] ⚠️ 本轮 {len(sold_fail)}/{len(targets_to_sell)} 项未能售出："
                f"{', '.join(sold_fail)}"
            )
        # 无待售商品的情形已在上方提前 return,故此处 targets_to_sell 必非空。
        # "无法判断"现已一并计入 sold_fail(见上方三分支),所以 sold_ok / sold_fail 双空
        # 只剩一种成因:外层循环还没处理完第一项就被停止指令打断。
        if sold_ok:
            mfaalog.info(f"[Arbitrage] 🎉 本轮实际售出 {len(sold_ok)} 项：{', '.join(sold_ok)}")
        elif sold_fail:
            mfaalog.warning(f"[Arbitrage] 🚫 本轮无一项成功售出({len(targets_to_sell)} 项全部失败),请检查上方失败原因。")
        else:
            mfaalog.info(f"[Arbitrage] ➖ 本轮 {len(targets_to_sell)} 项待售,一项都未及处理(多半是收到停止指令)。")
        return True

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
        prices = _col(_COL_PRICE)
        carts = _col(_COL_CART)

        # 名锚:名列内非数字文本 = 各商品行(与上子行同高),按 y 升序、近距去重
        anchors = []
        for t in sorted(names, key=lambda t: t["cy"]):
            cleaned = re.sub(r'[^\w一-龥]', '', t["text"])
            if cleaned and not cleaned.isdigit():
                if not any(abs(t["cy"] - a["cy"]) < 30 for a in anchors):
                    anchors.append({"name": cleaned, "cy": t["cy"]})
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

        results = []
        for i, row in enumerate(anchors):
            item_data = {"name": row["name"], "is_max_price": False, "target_cartridge": "",
                         "cart_score": 0.0, "cart_conflict": False, "alt_cartridge": ""}
            ny = row["cy"]                                    # 上子行(当前)y = 名锚 y
            next_ny = anchors[i + 1]["cy"] if i + 1 < len(anchors) else ny + gap_bound

            # 下子行(每月)y:本商品带内(next_ny 为界,防吸入下一商品上子行)、名字下方最近的价格行
            below_ys = sorted(
                t["cy"] for t in prices
                if ny + SUBROW_TOL < t["cy"] < next_ny and RE_PCT.search(t["text"])
            )
            mon_y = below_ys[0] if below_ys else None

            # 满价 = 今日溢价率 与 每月最高价档 相同(两子行都读到且有交集)
            top_pct = _row_pcts(ny)
            bot_pct = _row_pcts(mon_y) if mon_y is not None else set()
            if top_pct and bot_pct and (top_pct & bot_pct):
                item_data["is_max_price"] = True

            # 卡带:上子行(当前)组;满价时下子行(每月)是同柜台、理应同串(#B),两组各取组分并取
            # 组分高的一组整串。非满价不卖,仅取上子行(每月档与当前不同,交叉无意义)。
            up_str, up_sc = _cart_group_rescued(
                (t for t in carts if abs(t["cy"] - ny) <= SUBROW_TOL),
                context, screenshot, rescue_cfg, f"{row['name']}·当前")
            best_str, best_sc = up_str, up_sc
            if item_data["is_max_price"] and mon_y is not None:
                lo_str, lo_sc = _cart_group_rescued(
                    (t for t in carts if abs(t["cy"] - mon_y) <= SUBROW_TOL),
                    context, screenshot, rescue_cfg, f"{row['name']}·每月")
                if lo_str:                                    # 每月组也读到才交叉
                    # 号是去柜台的必需位:带号组优先(缺号组即便类型分更高也不能选,否则会像 07-25
                    # 实录——一子行救回号、另一子行没救回却因类型分高被选中→整项缺号被误跳)。
                    up_has, lo_has = bool(_tail_num(up_str)), bool(_tail_num(lo_str))
                    if lo_has and not up_has:
                        best_str, best_sc = lo_str, lo_sc
                    elif up_has == lo_has and lo_sc > up_sc:  # 两组同态(都带号/都缺号)→ 比组分
                        best_str, best_sc = lo_str, lo_sc
                    # 分歧判定同样走匹配式(2026-08-03):繁简互吃的两串生成同一条正则、指向同一
                    # 柜台,不算分歧——否则会对无害的 帶/带 差异打误导性告警并触发回退空跑。
                    item_data["cart_conflict"] = (
                        _cart_expected(up_str) != _cart_expected(lo_str)
                    )
                    if item_data["cart_conflict"]:
                        # 分歧回退候选(2026-08-03):两子行各自笃定却互斥时(实录组分双1.00,当前行误读
                        # 17/每月行12,首选进错柜台白跑),把未被选中的一串也带走,执行层金币验证失败后
                        # 可改试一次——把"抛硬币"变成"两个都试",真相仍由金币验证承担。
                        item_data["alt_cartridge"] = lo_str if best_str == up_str else up_str
            item_data["target_cartridge"] = best_str
            item_data["cart_score"] = best_sc

            results.append(item_data)

        return results
