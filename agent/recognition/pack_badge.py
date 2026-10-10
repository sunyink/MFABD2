import json
import math
import re
import time
from pathlib import Path
import numpy as np
from PIL import Image
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition
from maa.context import Context
import utils
from utils.num_list import parse_number_list

# ==============================================================================
# 🎖️ 卡带完成度徽章识别 (PackBadgeNotDone / PackUnnamedNotDone)
# ==============================================================================
# [用途]
# 快速卡带盒里每张可采集卡带下方有两枚圆形徽章：左「星」右「骷髅」。
# 游戏在对应采集类型完成后会给徽章打上一个白色对勾（勾从币底右下伸出）。
# 完成判据（用户口径）：**两枚都打勾 = 该卡带采集完成**；只要有任一枚没勾
# （彩色未采 / 灰白半采），都算未完成，需要进卡带采集。
# 另两类卡带直接忽略：下方无徽章的（不支持采集），以及币位图案不是星/骷髅的
# （租借卡等，币位是别的图案）——由图案比对裁决（见 [图案比对]）。
#
# [三态实测]（720p 截图，PIL HSV，H/S/V 均 0~255）
#   未采集   : 星币青绿(H≈109-120) + 骷髅币红(H≤15)，右下无勾  -> 勾区亮像素 0~2
#   采了一部分: 一枚已勾（灰白币+勾），另一枚彩色或灰白无勾      -> 已勾币勾区 11~13
#   采集完   : 两枚灰白 + 双勾                                  -> 勾区 11~13
# 勾区(币框右下外凸 12x7) V≥185 的像素数：有勾 11~13 / 无勾 0~2，间隔干净。
#
# [几何]（相对 PackFrame_Loc 锚点框左上角，模板 UI_*Pack.png = 114x78，
#  已在两张实机截图上用模板匹配复现核对：框 = 卡带蓝框左上角）
#   星币   = 框 + (28, 81) 26x26
#   骷髅币 = 框 + (60, 81) 26x26
#   勾区   = 币框 + (16, 25) 12x7   （币底右下外凸，含少量币环抗锯齿）
#   彩色区 = 币框 + (3, 3) 20x20    （币心图案，仅用于日志诊断）
#
# [节点用法] —— 作为 Collect_Pack_* 的 And 子识别，替换原 CheckCoolDown：
#   {
#       "recognition": "Custom",
#       "roi": "[Anchor]PackFrame_Loc",
#       "custom_recognition": "PackBadgeNotDone",
#       "custom_recognition_param": { "card_name": "Story_01_血骑士" }
#   }
# 命中 = 该卡带还没采完（至少一枚徽章没勾）。
# 上游框来自 [Anchor]PackFrame_Loc（Collect_LocatePackFrame_Loc* 登记的卡带框），
# 徽章都在框下方框外，由本识别按上面的相对偏移自行裁剪。
#
# [防死循环] 采集不完整的卡带勾不了满勾，本识别会持续命中。重进保护交给
# 节点自身的 "max_hit"（按任务运行计数），不在这里记状态。
#
# [参数]（custom_recognition_param，全部可省）
#   "card_name": str        仅用于日志
#   "debug": true           把徽章裁剪图存到 debug/pack_badge/ 供调阈值
#   "star_coin" / "skull_coin": [dx,dy,w,h]   覆盖默认几何
#   "check_min_px": int     勾判定像素数阈值，默认 6（实测有勾 11~13 / 无勾 0~2）
# ==============================================================================

# —— 默认几何（相对锚点框左上角）——
STAR_COIN = (28, 81, 26, 26)
SKULL_COIN = (60, 81, 26, 26)
# —— 相对币框左上角 ——
CHK_ZONE = (16, 25, 12, 7)      # 勾区：币底右下外凸
EMB_ZONE = (3, 3, 20, 20)       # 币心图案区
# —— 判定阈值（PIL HSV 0~255 刻度，实测见文件头）——
CHK_V_MIN, CHK_S_MAX, CHK_MIN_PX = 185, 90, 6
COL_S_MIN, COL_V_MIN = 50, 100          # 彩色（未采完的绿/红）
HUE_GREEN = (95, 150)                   # 星币青绿
HUE_RED = (0, 18, 230, 255)             # 骷髅币红（跨 0 环绕）
FACE_V_MIN, FACE_MIN_PX = 200, 25       # 币面存在（白币 V 很高，实测有币 140~330 / 无币 0）
# —— 图案比对（币心是否真的是星/骷髅，排除租借卡等异形图案）——
# 模板: Cartridges/badges/Badge_{Star,Skull}_{Raw,Done}.png (20x20 币心，实机 720p 裁剪)
# 表征: 255-V（反转亮度，星/骷髅图案在四种状态下都比白币面"暗"）
# 实测: 同图案 0.99~1.0；跨状态泛化(raw模板配done币) 星0.84~0.86/骷髅0.93；
#       最像的混淆(星币配骷髅模板)≤0.757；空白/异形图案≤0.29 -> 阈值 0.80
BADGE_TPL_TH = 0.80
_BADGE_TPL = None


def _badge_templates():
    """加载币面图案模板(模块级缓存)。找模板目录：install 布局=parents[2]/resource，
    仓库开发布局=parents[2]/assets/resource。"""
    global _BADGE_TPL
    if _BADGE_TPL is not None:
        return _BADGE_TPL
    out = {}
    root = Path(__file__).resolve().parents[2]
    for base in (root / "resource" / "base" / "image" / "Cartridges" / "badges",
                 root / "assets" / "resource" / "base" / "image" / "Cartridges" / "badges"):
        if base.is_dir():
            for p in sorted(base.glob("Badge_*.png")):
                hsv = np.array(Image.open(p).convert("RGB").convert("HSV")).astype(int)
                out[p.stem] = (255 - hsv[..., 2]).astype(float)
            break
    if not out:
        utils.mfaalog.error(
            "[PackBadge] ❌ 找不到币面图案模板目录 Cartridges/badges/Badge_*.png，"
            "图案比对不可用"
        )
    _BADGE_TPL = out
    return out


def _ncc_best(region, tpl):
    """在 region(币框 26x26) 内滑动 tpl(20x20) 取最大 NCC。"""
    th, tw = tpl.shape
    H, W = region.shape
    if H < th or W < tw:
        return -1.0
    t = tpl - tpl.mean()
    tn = math.sqrt((t * t).sum())
    if tn < 1e-6:
        return -1.0
    best = -1.0
    for dy in range(H - th + 1):
        for dx in range(W - tw + 1):
            p = region[dy:dy + th, dx:dx + tw]
            pm = p - p.mean()
            pn = math.sqrt((pm * pm).sum())
            if pn < 1e-6:
                continue
            v = float((pm * t).sum() / (pn * tn))
            if v > best:
                best = v
    return best


def pattern_scores(hsv, star_box, skull_box):
    """两枚币的图案得分 = 与各自图案模板(Raw/Done 两态取大)的最高 NCC。

    星币只许配星模板、骷髅币只许配骷髅模板——图案不对(租借卡等)得分掉到
    0.3 以下，阈值 BADGE_TPL_TH 卡在真图案(≥0.84)与最像混淆(≤0.76)之间。
    """
    tpls = _badge_templates()
    v = (255 - hsv[..., 2]).astype(float)
    out = {}
    for key, box in (("star", star_box), ("skull", skull_box)):
        x, y, w, h = (int(v) for v in box)
        region = v[max(0, y):y + h, max(0, x):x + w]
        cands = [t for n, t in tpls.items() if n.startswith(f"Badge_{key.capitalize()}_")]
        out[key] = round(max((_ncc_best(region, t) for t in cands), default=-1.0), 3)
    return out


def _zone(hsv, origin, box):
    """裁剪 hsv 图上的绝对区域，越界自动收缩。box=(x,y,w,h) 绝对坐标。"""
    x, y, w, h = box
    H, W = hsv.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, int(x + w)), min(H, int(y + h))
    if x1 <= x0 or y1 <= y0:
        return None
    return hsv[y0:y1, x0:x1]


def _shift(base, off):
    return (base[0] + off[0], base[1] + off[1], off[2], off[3])


def coin_report(hsv, coin_box, check_min_px=CHK_MIN_PX):
    """对单枚徽章出报告。checked 判据：勾区高亮像素 ≥ check_min_px。

    coin_seen 只回答"这个位置有没有一枚亮色圆币"（存在性）；
    币面图案是不是星/骷髅由 pattern_scores 的模板比对裁决，不在这里猜。
    """
    out = {"checked": False, "check_px": 0, "colored_px": 0, "face_px": 0,
           "coin_seen": False}
    chk = _zone(hsv, None, _shift(coin_box, CHK_ZONE))
    if chk is not None:
        V, S = chk[..., 2].astype(int), chk[..., 1].astype(int)
        out["check_px"] = int(((V >= CHK_V_MIN) & (S < CHK_S_MAX)).sum())
    emb = _zone(hsv, None, _shift(coin_box, EMB_ZONE))
    if emb is not None:
        Hc, S, V = emb[..., 0].astype(int), emb[..., 1].astype(int), emb[..., 2].astype(int)
        colored = (S >= COL_S_MIN) & (V >= COL_V_MIN)
        green = colored & (Hc >= HUE_GREEN[0]) & (Hc <= HUE_GREEN[1])
        red = colored & ((Hc <= HUE_RED[1]) | (Hc >= HUE_RED[2]))
        out["colored_px"] = int((green | red).sum())
        out["face_px"] = int(((V >= FACE_V_MIN) & (S < 70)).sum())
        out["coin_seen"] = out["face_px"] >= FACE_MIN_PX
    out["checked"] = out["check_px"] >= check_min_px
    return out


def analyze_badges(hsv, star_box, skull_box, check_min_px=CHK_MIN_PX, single_coin=False):
    """纯函数：给定整幅 HSV 图与两枚币的绝对框，返回判定报告（可离线测试）。

    返回勾/彩色/存在性统计；「图案是不是星/骷髅」不在此判定，
    由 pattern_scores（真模板比对）负责。

    single_coin=True（单币卡带，如活动卡「恶梦之冬」只有一个硬币、居中/横跨在
      星位与骷髅位之间）：只看 star_box 这一个框（调用方用 star_coin 覆盖成
      居中框），完全忽略骷髅位。判定：框内有币且勾了=采完；有币没勾=未采完；
      无币=不参与采集。
    """
    star = coin_report(hsv, star_box, check_min_px)
    skull = coin_report(hsv, skull_box, check_min_px)
    if single_coin:
        return {
            "star": star,
            "skull": skull,
            "no_badge": not star["coin_seen"],
            "need_collect": star["coin_seen"] and not star["checked"],
        }
    star_need = star["coin_seen"] and not star["checked"]
    skull_need = skull["coin_seen"] and not skull["checked"]
    return {
        "star": star,
        "skull": skull,
        "no_badge": not (star["coin_seen"] and skull["coin_seen"]),
        "need_collect": star_need or skull_need,
    }


PACK_NODE_RE = re.compile(r"^Collect_Pack_(?:Story|Character|Event)_\d+$")

# —— 强采清单（2026-10-02 本地新增，配合界面选项「强制采集卡带」）——
# 名单来自 action/string_processor.py 的 ForceListApply 写进节点 attach 的那份配置：
# 该动作按类"清场 + 只启用名单"，这里负责**徽章放行**——名单内的卡带不再问"要不要采"。
# 分类留空时该项不会出现在结果里 -> 该分类照旧走徽章判定（原逻辑）。
FORCE_APPLY_NODE = "Collect_ForceList_Apply"
_FORCE_KEY_BY_SECTION = {"Story": "StoryPack", "Character": "CharacterPack", "Event": "EventPack"}
_CARD_NAME_RE = re.compile(r"^(Story|Character|Event)_(\d+)")


def force_list_numbers(context, node_name=FORCE_APPLY_NODE):
    """读强采应用节点的 attach -> {"StoryPack": {3,5}, ...}（没填的类不出现）。

    读不到/没配置 -> {}，此时强采放行整体不生效，行为与改动前完全一致。
    """
    try:
        obj = context.get_node_object(node_name)
        attach = dict(getattr(obj, "attach", None) or {})
    except Exception:
        return {}
    out = {}
    for k, v in attach.items():
        nums = parse_number_list(v)
        if nums:
            out[k] = set(nums)
    return out


def card_in_force_list(card, force_map):
    """card_name（如 Character_3_美丽无望）是否落在强采名单里。"""
    if not force_map or not card:
        return False
    m = _CARD_NAME_RE.match(str(card))
    if not m:
        return False
    key = _FORCE_KEY_BY_SECTION.get(m.group(1))
    if not key:
        return False
    return int(m.group(2)) in force_map.get(key, set())


def disabled_pack_nodes(context):
    """当前被禁用的具名卡带节点 = 不参与采集的卡带（上游 enabled:false 或用户黑名单）。"""
    try:
        names = list(context.tasker.resource.node_list)
    except Exception:
        return []
    out = []
    for name in names:
        if not PACK_NODE_RE.match(str(name)):
            continue
        try:
            data = context.get_node_data(name)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("enabled") is False:
            out.append(name)
    return out


def is_blacklisted_slot(context, image, limit=6):
    """当前槽位的卡带是不是黑名单卡带。

    黑名单（采集卡带屏蔽）是把具名节点 `Collect_Pack_*_N` 打成 enabled:false。
    本识别按坐标工作、不知道自己面对的是第几张卡带，所以反向查：把被禁用的那几个
    节点临时 override 成启用后在当前槽位跑一次识别，谁的画面模板命中，当前槽位就是谁。
    run_recognition 对禁用节点会直接返回 None，必须靠 pipeline_override 临时启用。
    """
    nodes = disabled_pack_nodes(context)
    if not nodes:
        return False, []
    hit = []
    for name in nodes[:limit]:
        try:
            detail = context.run_recognition(
                name, image, {name: {"enabled": True}}
            )
        except Exception:
            detail = None
        if detail is not None and getattr(detail, "hit", False):
            hit.append(name)
    return bool(hit), hit



# —— 本地完成记录同步（2026-10-01 本地新增）——
# 缓存闸 Collect_QC_Skip_* 判"整类全完成"要求每张卡都有完成记录,而记录原本只在
# "退卡带"时由 MarkComplete 写入。这样一来"本来就双勾、从没被点进去采集"的卡就
# 永远没有记录,闸永远不成立(实测 Story_14/Story_19)。所以在本识别里顺手对齐:
#   · 看到双勾(已采完)          -> 写/刷新记录
#   · 看到未采完 / 不参与采集     -> 删掉记录(自愈:本地不该说它完成了)
CARD_CYCLE = "g_weekly"


def _sync_completion(card: str, done: bool) -> None:
    if not card or card == "?":
        return
    key = f"{card}@{CARD_CYCLE}"
    try:
        from utils.persistent_store import PersistentStore
    except Exception as e:
        utils.mfaalog.warning(f"[PackBadge] 无法加载存档模块,跳过完成度同步: {e}")
        return
    try:
        if done:
            had = PersistentStore.get(key, None)
            PersistentStore.set(key, time.strftime("%Y-%m-%d %H:%M:%S"))
            if not had:
                utils.mfaalog.info(f"[PackBadge] 📝 {card} 双勾已确认 -> 登记本地完成记录({CARD_CYCLE})")
            else:
                utils.mfaalog.debug(f"[PackBadge] 📝 {card} 双勾已确认 -> 刷新本地完成记录")
        else:
            data = PersistentStore.load()
            if key in data:
                data.pop(key, None)
                PersistentStore.save(data)
                utils.mfaalog.info(f"[PackBadge] 🧹 {card} 徽章未双勾 -> 撤销本地完成记录(自愈)")
    except Exception as e:
        utils.mfaalog.warning(f"[PackBadge] 完成度同步失败({card}): {e}")


@AgentServer.custom_recognition("PackBadgeNotDone")
class PackBadgeNotDone(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw) or "{}")
        except Exception as e:
            utils.mfaalog.error(f"[PackBadge] 参数解析失败: {e}")
            return None

        card = params.get("card_name", "?")
        try:
            roi = argv.roi
            rx, ry, rw, rh = roi.x, roi.y, roi.w, roi.h
        except Exception:
            rx = ry = rw = rh = 0
        if not rw or not rh:
            # 锚点框没解析出来就无法定位徽章。宁可大声报错漏采一次，
            # 也不能瞎命中把满周卡带全部重刷。
            utils.mfaalog.error(
                f"[PackBadge] ❌ {card}: roi 未解析到 PackFrame_Loc 锚点框"
                f"(roi={rx},{ry},{rw},{rh})，跳过该卡带的徽章判定"
            )
            return None

        # —— 强采放行（2026-10-02 本地新增）——
        # 用途：界面「强制采集卡带」把这张卡点名后，不再问"要不要采"，直接算未完成 -> 进卡带。
        # 场景：测试卡带地图切换/寻路，或本地周记录与游戏内状态不一致时硬采。
        # 前提安全：调用方 Collect_Pack_* 是 And[图标模板, 本识别]，走到这里说明图标已确认是这张卡。
        # 该类没填（不在 attach 里）-> card_in_force_list 返回 False -> 完全按原逻辑判定。
        try:
            _force_map = force_list_numbers(context)
        except Exception as e:
            _force_map = {}
            utils.mfaalog.warning(f"[PackBadge] 强采名单读取失败，按原逻辑判定: {e}")
        if card_in_force_list(card, _force_map):
            fb = tuple(params.get("star_coin", STAR_COIN))
            utils.mfaalog.info(
                f"[PackBadge] 🚀 {card} 在强采名单内 -> 无视徽章直接放行（强制采集，仅供测试/强采）"
            )
            _sync_completion(card, False)
            return CustomRecognition.AnalyzeResult(
                box=[rx + fb[0], ry + fb[1], fb[2], fb[3]],
                detail={"force_collect": True},
            )

        img = getattr(argv, "image", None)
        if img is None:
            utils.mfaalog.error(f"[PackBadge] ❌ {card}: 拿不到识别图像")
            return None
        # BGR -> HSV（PIL 刻度 0~255，与文件头实测一致）
        hsv = np.array(Image.fromarray(img[..., ::-1]).convert("HSV"))

        star_box = tuple(params.get("star_coin", STAR_COIN))
        skull_box = tuple(params.get("skull_coin", SKULL_COIN))
        rep = analyze_badges(
            hsv,
            (rx + star_box[0], ry + star_box[1], star_box[2], star_box[3]),
            (rx + skull_box[0], ry + skull_box[1], skull_box[2], skull_box[3]),
            int(params.get("check_min_px", CHK_MIN_PX)),
            bool(params.get("single_coin", False)),
        )

        if params.get("debug"):
            self._dump_debug(hsv, rx, ry, card, rep)

        def _tag(c):
            if not c["coin_seen"]:
                return "无币"
            if c["checked"]:
                return "已勾"
            if c["colored_px"] > 0:
                return "未采"
            return "无勾"

        s, k = rep["star"], rep["skull"]
        if rep["no_badge"]:
            # 币位连亮色圆币都没有 -> 不支持采集的卡带。原配置里这类多是
            # enabled:false（Collect_Pack_Story_20 / Collect_Pack_Character_8）。
            utils.mfaalog.warning(
                f"[PackBadge] ⚪ {card} 币位未见圆币，视为不参与采集，跳过"
                f"(星 face={s['face_px']} 骷髅 face={k['face_px']})"
            )
            _sync_completion(card, False)
            return None
        if rep["need_collect"]:
            utils.mfaalog.info(
                f"[PackBadge] 🟡 {card} 未采完: 星[{_tag(s)} chk={s['check_px']} col={s['colored_px']}] "
                f"骷髅[{_tag(k)} chk={k['check_px']} col={k['colored_px']}] -> 需要采集"
            )
            _sync_completion(card, False)
            return CustomRecognition.AnalyzeResult(
                box=[rx + star_box[0], ry + star_box[1], star_box[2], star_box[3]],
                detail={"need_collect": True, "star": s, "skull": k},
            )
        utils.mfaalog.debug(
            f"[PackBadge] ✅ {card} 已采完: 星[chk={s['check_px']}] 骷髅[chk={k['check_px']}] -> 跳过"
        )
        _sync_completion(card, True)
        return None

@AgentServer.custom_recognition("PackUnnamedNotDone")
class PackUnnamedNotDone(PackBadgeNotDone):
    """无名卡带兜底：当前槽位这张卡带没有任何具名节点（新卡带/改版卡带/模板缺失）。

    与 PackBadgeNotDone 的差别有三处：
      1. 图案比对：两枚币位必须真的画着星/骷髅（Badge_*_Raw/Done 模板，
         NCC ≥ BADGE_TPL_TH）——租借卡等不可采集卡带也可能有两枚圆币，
         但图案不是星/骷髅，一律忽略。
      2. 命中前先过黑名单：把被禁用的具名节点临时启用后在本槽位跑一次识别，
         谁的画面模板命中，这张就是黑名单卡带 -> 不命中（不采）。
      3. 不看 card_name（本来就没有），命中框回整张卡带框（点击落在卡带上）。
    排序上它排在 33 个具名候选项之后，只处理具名节点都没认领的槽位。
    """

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw) or "{}")
        except Exception as e:
            utils.mfaalog.error(f"[PackUnnamed] 参数解析失败: {e}")
            return None

        try:
            roi = argv.roi
            rx, ry, rw, rh = roi.x, roi.y, roi.w, roi.h
        except Exception:
            rx = ry = rw = rh = 0
        if not rw or not rh:
            utils.mfaalog.error(
                f"[PackUnnamed] ❌ roi 未解析到 PackFrame_Loc 锚点框(roi={rx},{ry},{rw},{rh})"
            )
            return None

        img = getattr(argv, "image", None)
        if img is None:
            utils.mfaalog.error("[PackUnnamed] ❌ 拿不到识别图像")
            return None
        hsv = np.array(Image.fromarray(img[..., ::-1]).convert("HSV"))

        star_box = tuple(params.get("star_coin", STAR_COIN))
        skull_box = tuple(params.get("skull_coin", SKULL_COIN))
        rep = analyze_badges(
            hsv,
            (rx + star_box[0], ry + star_box[1], star_box[2], star_box[3]),
            (rx + skull_box[0], ry + skull_box[1], skull_box[2], skull_box[3]),
            int(params.get("check_min_px", CHK_MIN_PX)),
        )
        s, k = rep["star"], rep["skull"]

        if rep["no_badge"] or not rep["need_collect"]:
            # 无币=不支持采集；双勾=采完。这两种都不该由本节点处理
            return None

        # 图案比对：两枚币位上真的画着星和骷髅才算可采集徽章。
        # 租借卡等不可采集卡带也可能有两枚圆币，但图案不是星/骷髅。
        th = float(params.get("badge_tpl_th", BADGE_TPL_TH))
        ps = pattern_scores(
            hsv,
            (rx + star_box[0], ry + star_box[1], star_box[2], star_box[3]),
            (rx + skull_box[0], ry + skull_box[1], skull_box[2], skull_box[3]),
        )
        if ps["star"] < th or ps["skull"] < th:
            utils.mfaalog.info(
                f"[PackUnnamed] ⚪ 币面图案不是星/骷髅(星={ps['star']} 骷髅={ps['skull']} "
                f"阈值={th})，视为不参与采集，跳过"
            )
            return None

        blacked, who = is_blacklisted_slot(context, img)
        if blacked:
            utils.mfaalog.info(
                f"[PackUnnamed] ⛔ 未双勾，但命中黑名单卡带 {who}，按屏蔽处理不采集"
            )
            return None

        utils.mfaalog.info(
            f"[PackUnnamed] 🟡 未被具名节点认领的卡带(槽位框 {rx},{ry}) 未采完: "
            f"星[chk={s['check_px']} col={s['colored_px']} 图案={ps['star']}] "
            f"骷髅[chk={k['check_px']} col={k['colored_px']} 图案={ps['skull']}] -> 需要采集"
        )
        return CustomRecognition.AnalyzeResult(
            box=[rx, ry, rw, rh],
            detail={"need_collect": True, "unnamed": True, "star": s, "skull": k},
        )

    def _dump_debug(self, hsv, rx, ry, card, rep):
        try:
            import os
            d = os.path.join("debug", "pack_badge")
            os.makedirs(d, exist_ok=True)
            ts = f"{time.time():.3f}".replace(".", "_")
            crop = hsv[ry:ry + 130, rx:rx + 114].astype(np.uint8)
            rgb = Image.fromarray(crop, "HSV").convert("RGB")
            rgb.save(os.path.join(d, f"{ts}_{card}.png"))
        except Exception as e:
            utils.mfaalog.warning(f"[PackBadge] 调试图保存失败: {e}")
