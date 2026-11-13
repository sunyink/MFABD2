"""顶栏货币读数：钻石、金币、天赋神药；钻石的免费 / 付费从钻石弹窗读。

背包里没有货币栏，这三样只能在主界面或箱庭的顶栏读。顶栏随数字位数整排挪动（箱庭居中排），
所以先用模板认出三个图标，再读各自右边的数字；钻石弹窗也是点认出的钻石图标打开的。
付费钻石只有玩家花钱才会变：流程开头读一次免费 / 付费，之后只读总数，免费 = 总数 − 付费。
节点在 Global.json 的 Agt_Currency_*；实测与截图在 priv doc/agent/ledger/verify/2026-11-13-施工前核对/货币栏/。
"""

import json
import re
import threading
import time

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.inventory_store import save_currency_reading, utc_now
from utils.ocr_read import clean
from utils.persistent_store import PersistentStore


ICON_NODES = (
    ("diamond", "Agt_Currency_Diamond_Icon_Tpl"),
    ("gold", "Agt_Currency_Gold_Icon_Tpl"),
    ("tonic", "Agt_Currency_Tonic_Icon_Tpl"),
)
NUMBER_NODE = "Agt_Currency_Number_Ocr"
CLICK_NODE = "Agt_Currency_Diamond_Click"
POPUP_NODE = "Agt_Currency_Diamond_Popup_Ocr"
EXPAND_NODE = "Agt_Currency_Diamond_Expand_Tpl"
DETAIL_NODE = "Agt_Currency_Diamond_Detail_Ocr"
FREE_NODE = "Agt_Currency_Diamond_Free_Ocr"
PAID_NODE = "Agt_Currency_Diamond_Paid_Ocr"
CLOSE_NODE = "Agt_Currency_Diamond_Close"

# 千分位只认三位一组；分隔符收逗号与点（模拟器主界面的逗号常被认成点）。
# 不收不带分隔符的长数字：图标被并进数字时会读成「6137227」这类，宁可读不到也不读错。
_GROUPED = r"(0|[1-9][0-9]{0,2}(?:[,.][0-9]{3})*)"
_OWNED = r"(?:[拥擁]有|持有)" + _GROUPED + r"[个個]"
_WAIT_SECONDS = 3.0
_POLL_SECONDS = 0.15

_PAID_LOCK = threading.Lock()
_PAID_BY_ACCOUNT: dict = {}


def parse_grouped(text: str):
    match = re.fullmatch(_GROUPED, clean(text))
    return int(re.sub(r"\D", "", match.group(1))) if match else None


def parse_owned(text: str):
    match = re.fullmatch(_OWNED, clean(text))
    return int(re.sub(r"\D", "", match.group(1))) if match else None


def _rect(box):
    if box is None:
        return None
    if hasattr(box, "x"):
        return [int(box.x), int(box.y), int(box.w), int(box.h)]
    return [int(value) for value in box[:4]] if len(box) >= 4 else None


def _ocr_items(result):
    """OCR 结果里的（文字, 框）；没命中返回空。"""
    if result is None or not result.hit:
        return []
    items = getattr(result, "filtered_results", None) or getattr(result, "all_results", None) or []
    pairs = []
    for item in items:
        text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        box = _rect(item.get("box") if isinstance(item, dict) else getattr(item, "box", None))
        if text and box:
            pairs.append((str(text), box))
    return pairs


def _screencap(context):
    image = context.tasker.controller.post_screencap().wait().get()
    if image is None or not getattr(image, "size", 0):
        raise RuntimeError("截图失败")
    return image


def locate_icons(context, image) -> dict:
    icons = {}
    for name, node in ICON_NODES:
        result = context.run_recognition(node, image)
        icons[name] = _rect(result.box) if result is not None and result.hit else None
    return icons


def number_rois(icons: dict, attach: dict, width: int, height: int) -> dict:
    """每个认出的图标右边一段：到下一个认出的图标为止，最右那个取 tail_width。"""
    found = sorted((box[0], name, box) for name, box in icons.items() if box)
    rois = {}
    for index, (_, name, box) in enumerate(found):
        x = box[0] + box[2] + 1
        end = found[index + 1][2][0] - 2 if index + 1 < len(found) else x + attach["tail_width"]
        center = box[1] + box[3] // 2
        y = max(0, center - attach["half_height"])
        bottom = min(height, center + attach["half_height"])
        end = min(width, end)
        if end - x >= 8 and bottom - y >= 8:
            rois[name] = [x, y, end - x, bottom - y]
    return rois


def read_number(context, image, roi: list, max_lead: int):
    """roi 里紧挨图标开头、能完整解析的数字中取最宽的；读不到返回 None。"""
    result = context.run_recognition(NUMBER_NODE, image, {NUMBER_NODE: {"roi": roi}})
    best = None
    for text, box in _ocr_items(result):
        value = parse_grouped(text)
        if value is None or box[0] - roi[0] > max_lead:
            continue
        if best is None or box[2] > best[0]:
            best = (box[2], value)
    return None if best is None else best[1]


def read_bar(context, image) -> dict:
    attach = context.get_node_object(NUMBER_NODE).attach
    height, width = image.shape[:2]
    icons = locate_icons(context, image)
    rois = number_rois(icons, attach, width, height)
    values = {name: (read_number(context, image, rois[name], attach["max_lead"]) if name in rois else None)
              for name, _ in ICON_NODES}
    return {"values": values, "icons": icons}


def _wait(context, node, *, present: bool = True):
    """轮询截图直到节点命中（present）或不再命中；返回（截图, 识别结果）或（截图, None）。"""
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        image = _screencap(context)
        result = context.run_recognition(node, image)
        hit = result is not None and result.hit
        if hit == present:
            return image, result
        if time.monotonic() >= deadline or context.tasker.stopping:
            return image, None
        time.sleep(_POLL_SECONDS)


def _read_owned(context, image, node):
    values = {parse_owned(text) for text, _ in _ocr_items(context.run_recognition(node, image))}
    values.discard(None)
    return values.pop() if len(values) == 1 else None


def read_diamond_detail(context, icon_box: list) -> dict:
    """点钻石图标 → 等弹窗 → 点 ⊕ → 等「详细拥有量」→ 读免费 / 付费 → 点 X 关掉并确认已关。"""
    detail: dict = {"free": None, "paid": None, "error": None, "closed": None}
    opened = False
    try:
        context.run_action(CLICK_NODE, box=icon_box)
        image, popup = _wait(context, POPUP_NODE)
        if popup is None:
            detail["error"] = "popup_not_open"
            return detail
        opened = True
        expand = context.run_recognition(EXPAND_NODE, image)
        if expand is None or not expand.hit:
            detail["error"] = "expand_not_found"
            return detail
        context.run_action(EXPAND_NODE, box=_rect(expand.box))
        image, shown = _wait(context, DETAIL_NODE)
        if shown is None:
            detail["error"] = "detail_not_shown"
            return detail
        detail["free"] = _read_owned(context, image, FREE_NODE)
        detail["paid"] = _read_owned(context, image, PAID_NODE)
        if detail["free"] is None or detail["paid"] is None:
            detail["error"] = "detail_unreadable"
        return detail
    except Exception as exc:
        detail["error"] = f"exception: {exc}"
        return detail
    finally:
        if opened:
            try:
                context.run_action(CLOSE_NODE)
                _, still = _wait(context, POPUP_NODE, present=False)
                detail["closed"] = still is not None
            except Exception as exc:
                detail["closed"] = False
                mfaalog.warning(f"[Currency] ⚠️ 关闭钻石弹窗异常：{exc}")


def read_currency(context, *, diamond_detail=None) -> dict:
    """读顶栏三项。diamond_detail：True 点开弹窗读免费 / 付费；False 不读；
    None 按本进程是否已读过本账号的付费钻石决定（没读过才读）。

    读不到的项为 None，不抛异常（截图失败除外，由调用方接住）。付费钻石沿用时 diamond_paid_source="carried"。
    """
    account = PersistentStore._current_account_id
    image = _screencap(context)
    bar = read_bar(context, image)
    values = bar["values"]
    record = {
        "observed_at": utc_now(),
        "gold": values["gold"],
        "diamond": values["diamond"],
        "tonic": values["tonic"],
        "diamond_free": None,
        "diamond_paid": None,
        "diamond_paid_source": None,
        "icons_found": sorted(name for name, box in bar["icons"].items() if box),
        "detail_error": None,
        "detail_closed": None,
    }
    with _PAID_LOCK:
        carried = _PAID_BY_ACCOUNT.get(account)
    want = diamond_detail is True or (diamond_detail is None and carried is None)
    if want and bar["icons"].get("diamond"):
        detail = read_diamond_detail(context, bar["icons"]["diamond"])
        record["detail_error"] = detail["error"]
        record["detail_closed"] = detail["closed"]
        free, paid = detail["free"], detail["paid"]
        total = record["diamond"]
        if paid is not None and free is not None and (total is None or free + paid == total):
            record.update(diamond_free=free, diamond_paid=paid, diamond_paid_source="read")
            if total is None:
                record["diamond"] = free + paid
            with _PAID_LOCK:
                _PAID_BY_ACCOUNT[account] = paid
        elif paid is not None and free is not None:
            record["detail_error"] = record["detail_error"] or "detail_sum_mismatch"
    elif want:
        record["detail_error"] = "diamond_icon_not_found"
    if record["diamond_paid_source"] is None and carried is not None and record["diamond"] is not None:
        free = record["diamond"] - carried
        if free >= 0:
            record.update(diamond_free=free, diamond_paid=carried, diamond_paid_source="carried")
    return record


def _summary(record: dict) -> str:
    def shown(value):
        return "?" if value is None else f"{value:,}"

    diamond = shown(record["diamond"])
    if record["diamond_paid"] is not None:
        source = "读" if record["diamond_paid_source"] == "read" else "沿用"
        diamond += f"（免费 {shown(record['diamond_free'])} + 付费 {shown(record['diamond_paid'])}，付费{source}）"
    return f"钻石 {diamond}、金币 {shown(record['gold'])}、天赋神药 {shown(record['tonic'])}"


@AgentServer.custom_action("CurrencyRead")
class CurrencyRead(CustomAction):
    """读顶栏货币并写一条读数事件。参数：label（必填）、diamond_detail（true/false/null，默认 null）。

    只读：除钻石弹窗的打开、展开、关闭外不点别的。读不到、写不进都只记日志并返回 True，不打断所在流程。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = argv.custom_action_param
            if not isinstance(params, dict):
                params = json.loads(str(params or "{}"))
            label = params.get("label")
            if not isinstance(label, str) or not label.strip():
                mfaalog.error("[Currency] ❌ 缺少 label，本次不读")
                return True
            if not sync_from_context(context, where="CurrencyRead"):
                mfaalog.warning("[Currency] ⚠️ 账号未确认，本次不读")
                return True
            record = read_currency(context, diamond_detail=params.get("diamond_detail"))
            record["label"] = label.strip()
            saved = save_currency_reading(record)
            mfaalog.info(f"[Currency] {record['label']}：{_summary(record)}"
                         + ("" if saved else "（⚠️ 存档未写入）"))
            if record["detail_error"]:
                mfaalog.warning(f"[Currency] ⚠️ 钻石免费 / 付费未读成：{record['detail_error']}")
            if record["detail_closed"] is False:
                mfaalog.warning("[Currency] ⚠️ 钻石弹窗可能没关上，后续页面由所在流程自行恢复")
        except Exception as exc:
            mfaalog.error(f"[Currency] ❌ 货币读数异常：{exc}")
        return True
