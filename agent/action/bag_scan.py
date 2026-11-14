"""通用背包仓检：启动器 BagScan、扫描核心 BagScanRun，以及供 py 主控调用的 run_bag_scan。

pipeline 侧在 assets/resource/*/pipeline/BagScan.json：开包、复位、排序、点开/关闭详情、
翻页、原路返回都是节点；整页读图在 utils/bag_cells.py（把截图切成格子、每格一次同全部图标比对），
这里只把两边接起来：按节点里的排布与扫描类型组好参数，交给 utils/bag_scan_engine.py 决定点哪几个格子、
读到的数怎么记、什么时候能判 0。设计见 priv doc/agent/bag/背包仓检_设计.md。
"""

from collections import OrderedDict
import copy
from functools import lru_cache
import json
import time
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.bag_cells import Grid, icon_pack, item_table, read_page
from utils.bag_detail import (DetailLayout, FadeWatch, anchor_of, at_anchor, crop, plain_quantity, same_text,
                              split_quantity, stitch, text_box)
from utils.bag_scan_engine import BagScanEngine, Reading, Section
from utils.inventory_store import save_scan_record, set_inventory_quantities, utc_now
from utils.name_i18n import canon
from utils.ocr_item_name import clean, item_aliases
from utils.ocr_read import read_texts


__all__ = ["BagScan", "BagScanRun", "get_scan", "profile_names", "run_bag_scan"]

ENTRY = "BagScan_Entry"
RUN = "BagScan_Run"
OPEN, READY = "BagScan_Detail_Open", "BagScan_Detail_Ready"
CLOSE, CLOSED = "BagScan_Detail_Close", "BagScan_Detail_Closed"
PAGE, MOVED, BOTTOM = "BagScan_Page_Next", "BagScan_Page_Moved", "BagScan_Page_Bottom"
SWIPE = "Agt_BagScan_Page_Swipe"
RESET, RESET_EXIT = "BagScan_TabReset", "BagScan_TabReset_Egress"
NAME_OCR, QUANTITY_OCR = "Rec_BagScan_Name_Ocr", "Rec_BagScan_Quantity_Ocr"
# 详情弹窗的新读法：识别带与阈值在 Agt_BagScan_Detail；只识别的 OCR 节点；消耗品页签亮不亮（不亮 = 弹窗遮罩在）。
DETAIL, TEXT_REC, TAB_LIT = "Agt_BagScan_Detail", "Rec_BagScan_Text_Rec", "Rec_BagScan_ConsTab_Lit_Clr"
# 扫描类型、识别节点与各项上限在 Agt_BagScan_Config；列表的排布与图集在 Agt_BagScan_Grid（按资源包覆盖）。
CONFIG, GRID = "Agt_BagScan_Config", "Agt_BagScan_Grid"
OTHER = "other"     # 物品表里目录外物品（活动道具、亲密饮品）的档案名

_SCANS = OrderedDict()


def _bag_items():
    """tools/gen_bag_templates.py 按物品表生成的物品清单：[{cn, tw, alias, profile, kind, icon}]。"""
    return item_table()["items"]


def profile_names(*profiles) -> list:
    """这些档案（ingredients / dishes / growth / craft）里的目录物品，按物品表的顺序。"""
    return [item["cn"] for item in _bag_items() if item["profile"] in profiles]


@lru_cache(maxsize=1)
def _names():
    """读名用的名录与同名表：(OCR 写法 → 规范名, 与目录外物品同名的目录物品)。

    共用名录（食材等，含简繁异名）也服务商店 OCR，不往里加东西；这里另起一份，只给背包仓检用。
    目录外物品的规范名带 # 前缀：读到它们只说明「这格不是要找的」。一个写法对上一个目录物品和一个目录外物品时
    （简体「牛奶」），按目录物品收，并记进同名表——扫描核心要图标也认定才采信。两个目录物品同一个写法的不收。
    """
    aliases = dict(item_aliases())
    seen = {}
    for item in _bag_items():
        canonical = item["cn"] if item["profile"] != OTHER else "#" + item["cn"]
        # alias：游戏物品表里与项目规范名不同的简体原名（如「光圣石」→ 规范名「光明圣石」）
        for text in {item["cn"], item.get("tw") or "", *item.get("alias", [])} - {""}:
            seen.setdefault(clean(text), set()).add(canonical)
    ambiguous = set()
    for text, names in seen.items():
        catalog = sorted(name for name in names if not name.startswith("#"))
        if len(catalog) == 1:
            if aliases.get(text, catalog[0]) == catalog[0]:
                aliases[text] = catalog[0]
                if len(names) > 1:
                    ambiguous.add(catalog[0])
        elif not catalog:
            aliases.setdefault(text, sorted(names)[0])
    return aliases, frozenset(ambiguous)


def get_scan(scan_id):
    """本进程最近 16 次扫描记录的副本；进程重启后只能读存档里的摘要。"""
    record = _SCANS.get(scan_id)
    return copy.deepcopy(record) if record is not None else None


def _remember(record):
    _SCANS[record["scan_id"]] = copy.deepcopy(record)
    _SCANS.move_to_end(record["scan_id"])
    while len(_SCANS) > 16:
        _SCANS.popitem(last=False)


def _box(value):
    if hasattr(value, "x"):
        return [int(value.x), int(value.y), int(value.w), int(value.h)]
    return [int(item) for item in value]


def _ocr_items(detail):
    return getattr(detail, "filtered_results", None) or getattr(detail, "all_results", None) or []


def _reading(detail):
    """从 BagScan_Detail_Ready（And）的识别详情取名称与数量两段 OCR；子识别按节点名对应，取不到按顺序。"""
    best = getattr(detail, "best_result", None)
    subs = list(getattr(best, "sub_results", None) or [])
    by_name = {getattr(sub, "name", ""): sub for sub in subs}
    name_detail = by_name.get(NAME_OCR) or (subs[0] if subs else None)
    quantity_detail = by_name.get(QUANTITY_OCR) or (subs[1] if len(subs) > 1 else None)
    if name_detail is None or quantity_detail is None:
        return None
    return Reading(
        names=[(str(getattr(item, "text", "")), float(getattr(item, "score", 0))) for item in _ocr_items(name_detail)],
        quantities=[(str(getattr(item, "text", "")), float(getattr(item, "score", 0)), _box(item.box))
                    for item in _ocr_items(quantity_detail)],
    )


def _ran(detail, node):
    return next((item for item in (detail.nodes if detail is not None else []) if item.name == node), None)


class MaaBagDevice:
    """把扫描核心要用的动作落到 BagScan.json 的节点上；坐标与轮询参数都在节点里，可按资源包覆盖。

    点开详情（设计 §17）：节点只管点击；py 截帧等弹窗出现动画走完——遮罩在（消耗品页签不亮）、右上角「×」回到终态
    （与本趟记下的参照一致）、名称与数量两处与上一帧一样——再在那一帧上按颜色定出名称、数量的范围，拼成一行只识别一次；
    读完关闭前再截一帧核对文字没变，才交出读数。等的是画面状态，不是钟点。
    哪一步不成（版式没见过、颜色没找到字、拼读拆不开、一直等不到走完）就退回原来的检测+识别节点，扫描核心照旧两帧复核。
    """

    def __init__(self, context: Context, ready_node: str, detail: dict):
        self.context = context
        self.ready_node = ready_node
        self.layout = DetailLayout.from_attach(detail)
        self.open_timeout = detail.get("open_timeout", 2000) / 1000
        self.settle_timeout = detail.get("timeout", 5000) / 1000
        self.interval = detail.get("interval", 30) / 1000
        self.anchor = None      # 本趟记下的终态「×」：第一次读完核对通过时记，窗口缩放变了会在核对时换掉

    def stopping(self):
        return bool(self.context.tasker.stopping)

    def capture(self):
        image = self.context.tasker.controller.post_screencap().wait().get()
        if image is None or not image.size:
            raise RuntimeError("背包截图失败")
        return image

    def _recognize(self, node, image, override=None):
        result = self.context.run_recognition(node, image, override or {})
        if result is None:
            raise RuntimeError(f"识别未执行: {node}")
        return result

    def list_ready(self, image):
        return bool(self._recognize(self.ready_node, image).hit)

    def _settle(self, opening):
        """截帧等详情弹窗淡入走完。返回 (结论, 截图)：

        settled：遮罩在、「×」回到终态（有参照时）、文字与上一帧一样；还没有参照时要文字与「×」连续三帧不变
        （截图偶尔连着两次给同一帧，单看「和上一帧一样」不够）；
        opened：遮罩在，但到上限也没等到走完（参照可能已不对，丢掉，下一格重新记）；None：弹窗不在——opening 为真时
        （刚点过）等 open_timeout 让它出来，否则（弹窗本该开着）一帧就下结论。
        """
        watch = FadeWatch(self.layout, self.anchor)
        started, masked, image = time.monotonic(), False, None
        while not self.stopping():
            tick = time.monotonic()
            image = self.capture()
            if not self._recognize(TAB_LIT, image).hit:
                masked = True
                if watch.feed(image):
                    return "settled", image
            elif masked or not opening:
                return None, image
            elapsed = time.monotonic() - started
            if not masked and elapsed > self.open_timeout:
                return None, image
            if elapsed > self.settle_timeout:
                self.anchor = None
                return "opened", image
            time.sleep(max(0.0, self.interval - (time.monotonic() - tick)))
        return None, image

    def _rec(self, image):
        """只识别一块裁好的文字图（Rec_BagScan_Text_Rec，only_rec）。"""
        result = self._recognize(TEXT_REC, image)
        return [(str(getattr(item, "text", "")), float(getattr(item, "score", 0))) for item in _ocr_items(result)]

    def _node_names(self, image):
        result = self._recognize(NAME_OCR, image)
        return [(str(getattr(item, "text", "")), float(getattr(item, "score", 0))) for item in _ocr_items(result)]

    def _read_nodes(self, image, quantity):
        """退回原来的检测+识别节点。读数不算核实过，扫描核心照旧两帧复核。"""
        if quantity:
            result = self._recognize(READY, image)
            if result.hit:
                return _reading(result)
        return Reading(names=self._node_names(image))

    def _read_frame(self, image, quantity):
        """在走完的那一帧上读：按颜色定出名称（与数量）的范围；要数量时两段拼成一行只识别一次，拆不开再分开读。
        返回 (读数, 文字框)；颜色没找到字时退回原节点，文字框为空。"""
        name_box = text_box(image, self.layout.name, "white", self.layout)
        quantity_box = text_box(image, self.layout.quantity, "blue", self.layout, top_line=True) if quantity else None
        if name_box is None or (quantity and quantity_box is None):
            return self._read_nodes(image, quantity), ()
        boxes = (name_box, quantity_box)
        if not quantity:
            return Reading(names=self._rec(crop(image, name_box)), settled=True), boxes
        texts = self._rec(stitch(image, [name_box, quantity_box]))
        parts = split_quantity("".join(text for text, _ in texts))
        if parts is not None:
            score = min(score for _, score in texts)
            return Reading(names=[(parts[0], score)], quantities=[(parts[1], score, list(quantity_box))],
                           settled=True), boxes
        quantities = [(plain_quantity(text), score, list(quantity_box)) for text, score in self._rec(crop(image, quantity_box))]
        return Reading(names=self._rec(crop(image, name_box)), quantities=quantities, settled=True), boxes

    def _read_settled(self, image, quantity):
        """读判定走完的那一帧，读完关闭前再截一帧核对：文字处与读的那帧一样（这时离判定已过了一次识别的时间）才交出读数，
        并把这一帧的「×」记成本趟参照；不一样说明读的那帧还没走完，换这一帧再读（最多两次），仍不行退回原节点。"""
        for _ in range(2):
            reading, boxes = self._read_frame(image, quantity)
            if not reading.settled:
                return reading
            later = self.capture()
            if not self._recognize(TAB_LIT, later).hit and same_text(image, later, boxes, self.layout):
                if not at_anchor(later, self.anchor, self.layout):
                    self.anchor = anchor_of(later, self.layout)
                return reading
            image = later
        return self._read_nodes(image, quantity)

    def open_detail(self, box, quantity=True):
        """点开一格并读。None = 弹窗没出来（是否再点由扫描核心先看列表还在不在再定）。"""
        self.context.run_task(OPEN, {OPEN: {"target": list(box)}})
        state, image = self._settle(opening=True)
        if state is None:
            return None
        return self._read_settled(image, quantity) if state == "settled" else self._read_nodes(image, quantity)

    def peek_detail(self, box):
        """点开一格，只读名称：认不出的格子多半不是要找的，用不着数量；是待查物品时扫描核心再要数量。"""
        return self.open_detail(box, quantity=False)

    def read_detail(self):
        """弹窗开着时再读一帧名称与数量；弹窗不在返回 None。"""
        state, image = self._settle(opening=False)
        if state is None:
            return None
        return self._read_settled(image, True) if state == "settled" else self._read_nodes(image, True)

    def read_names(self):
        state, image = self._settle(opening=False)
        if state is None:
            return []
        return (self._read_settled(image, False) if state == "settled" else self._read_nodes(image, False)).names

    def read_quantity_scaled(self):
        texts, _, _ = read_texts(self.context, QUANTITY_OCR, self.capture(), retry=True)
        return texts

    def close_detail(self):
        return _ran(self.context.run_task(CLOSE), CLOSED) is not None

    def next_page(self):
        detail = self.context.run_task(PAGE)
        if _ran(detail, MOVED) is not None:
            return "moved"
        return "bottom" if _ran(detail, BOTTOM) is not None else "failed"

    def swipe(self):
        """不经 SmartAction 直接下滑一页，只回答滑动动作是否真的执行了；画面动没动由扫描核心自己比。"""
        node = _ran(self.context.run_task(SWIPE), SWIPE)
        return bool(node is not None and node.action is not None and node.action.success)

    def reset_to_top(self):
        return _ran(self.context.run_task(RESET), RESET_EXIT) is not None


def _scope_keys(raw, scopes: dict) -> list:
    """扫描类型参数（一个键如 "A"，或一组如 ["A", "C"]）→ 去重后的键列表。键在 Agt_BagScan_Config.attach.scopes 里定义。"""
    keys = [raw] if isinstance(raw, str) else list(raw or [])
    keys = list(dict.fromkeys(keys))
    if not keys or not all(isinstance(key, str) and key in scopes for key in keys):
        raise ValueError(f"未知扫描类型: {raw}（可用：{'、'.join(scopes)}）")
    return keys


def _targets(raw, names):
    if raw in (None, "", "all"):
        return list(names)
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ValueError("targets 只能是 all 或名称列表")
    return list(dict.fromkeys(canon(name.strip()) for name in raw if name.strip()))


def _plan(config: dict, keys: list):
    """按扫描类型列出要找的物品与各自连成的段：(物品名列表, [Section])。

    segment：head = 从列表开头起的一段，走出这一段就结清；run = 中间连成一段，进去再出来就结清；
    tail（或不写）= 没有可靠的段尾，扫到列表底。几种类型一起扫时各段各自结清，全部结清或到底即停。
    """
    names, sections = [], []
    for key in keys:
        scope = config["scopes"][key]
        members = profile_names(*scope["profiles"])
        names += members
        if scope.get("segment") in ("head", "run"):
            sections.append(Section(name=scope.get("title", key), members=frozenset(members), kind=scope["segment"],
                                    evidence=scope.get("zero_evidence", "section_end")))
    return list(dict.fromkeys(names)), sections


def _grid(attach: dict) -> Grid:
    options = {key: attach[key] for key in ("footer", "row_offset", "reach", "shift", "known", "margin", "empty_std")
               if key in attach}
    return Grid(area=tuple(attach["area"]), columns=int(attach["columns"]), left=float(attach["left"]),
                pitch=tuple(attach["pitch"]), **options)


def _label(raw):
    label = str(raw or "").strip()
    if not 1 <= len(label) <= 40:
        raise ValueError("label 必须是 1～40 字的扫描用途")
    return label


def _log(level, text):
    getattr(mfaalog, level)(f"[BagScan] {text}")


_UNKNOWN_TEXT = {
    "template_missing": "不在物品表里", "detail_unreadable": "详情未核实", "continuity_broken": "翻页接不上",
    "cell_unverified": "有格子没核实", "persist_failed": "存档写入失败", "page_limit": "翻页上限",
    "timeout": "超时", "stopped": "任务停止", "state_lost": "列表状态丢失", "error": "扫描出错",
    "not_scanned": "没进背包核对",
}


def unknown_text(unknown):
    """{名称: 原因码} → 按原因分组的一行说明，供日志列出未知物品。"""
    groups = {}
    for name, reason in unknown.items():
        groups.setdefault(reason, []).append(name)
    return "；".join(f"{_UNKNOWN_TEXT.get(reason, reason)}：{'、'.join(names)}" for reason, names in groups.items())


def _save_absent(record, reference, absent_by):
    """判 0 写进存档才算数；没写进去的改记未知，扫描随之不完整。absent_by：{判 0 依据: [名称]}。"""
    failed = []
    for evidence, names in absent_by.items():
        try:
            saved = set_inventory_quantities(dict.fromkeys(names, 0), "bag_absent", {**reference, "evidence": evidence})
        except Exception as exc:
            mfaalog.warning(f"[BagScan] 判 0 写入异常: {exc}")
            saved = False
        if not saved:
            failed += names
    if failed:
        record["absent"] = sorted(set(record["absent"]) - set(failed))
        record["unknown"] = {**record["unknown"], **dict.fromkeys(failed, "persist_failed")}
        raise RuntimeError("判 0 写入失败")


@AgentServer.custom_action("BagScanRun")
class BagScanRun(CustomAction):
    """扫描核心：BagScan_Run 节点。只观察、不作废任何旧量；完整与否只看记录里的 complete。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        raw = argv.custom_action_param
        params = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
        record = {"scan_id": params.get("scan_id") or uuid4().hex, "label": str(params.get("label") or "未指定"),
                  "note": params.get("note"), "started_at": utc_now(), "job_id": None,
                  "scope": [], "target_count": 0, "complete": False,
                  "termination": "error", "continuity": "broken", "pages": 0,
                  "quantities": {}, "absent": [], "unknown": {}, "cells": {}, "timing_ms": {}}
        if isinstance(params.get("origin"), dict):
            record["origin"] = params["origin"]   # 库存记账的位置触发的盘点：哪个节点、哪个任务、什么类型
        if not sync_from_context(context, where="BagScanRun"):
            return False
        try:
            record["job_id"] = context.get_task_job().job_id
            record["label"] = _label(record["label"])
            config = context.get_node_object(CONFIG).attach
            layout = context.get_node_object(GRID).attach
            record["scope"] = _scope_keys(params.get("scope", "A"), config["scopes"])
            names, sections = _plan(config, record["scope"])
            targets = _targets(params.get("targets"), names)
            record["target_count"] = len(targets)
            reference = {"scan_id": record["scan_id"], "label": record["label"]}
            grid, pack = _grid(layout), icon_pack(layout["icons"])
            aliases, ambiguous = _names()
            loose_kinds = frozenset(config.get("loose_kinds", ()))

            def persist(page, quantities):
                if not set_inventory_quantities(quantities, "bag_detail", {**reference, "page": page}):
                    raise RuntimeError("库存写入失败")

            engine = BagScanEngine(
                MaaBagDevice(context, config["ready_node"], context.get_node_object(DETAIL).attach), lambda image: read_page(image, grid, pack), targets,
                known_names=frozenset(item["cn"] for item in _bag_items() if item["profile"] != OTHER),
                grid_roi=grid.area, row_height=grid.pitch[1], columns=grid.columns, footer_rows=grid.footer,
                sections=sections, loose_kinds=loose_kinds,
                loose_names=frozenset(item["cn"] for item in _bag_items()
                                      if item["profile"] != OTHER and item["kind"] in loose_kinds),
                ambiguous=ambiguous, aliases=aliases,
                max_pages=config["max_pages"], timeout_seconds=config["timeout_seconds"],
                confirm_frames=config["confirm_frames"], box_tolerance=config["box_tolerance"],
                continuity_threshold=config["continuity_threshold"], on_page=persist, log=_log)
            result = engine.run()
            record.update({key: result[key] for key in (
                "complete", "termination", "continuity", "pages", "quantities", "absent", "unknown", "cells",
                "timing_ms")})
            if result["error"]:
                record["error"] = result["error"]
            if record["absent"]:
                _save_absent(record, reference, result["absent_by"])
        except Exception as exc:
            record["error"] = str(exc)
            record["complete"] = False
            mfaalog.warning(f"[BagScan] 扫描提前结束: {exc}")
        finally:
            record["finished_at"] = utc_now()
            try:
                saved = save_scan_record(record)
                record["seq"] = saved.get("seq") if saved else None
                if saved is None:
                    mfaalog.error("[BagScan] 扫描记录写入失败")
            except Exception as exc:
                mfaalog.error(f"[BagScan] 扫描记录存档异常: {exc}")
            _remember(record)
        timing, cells = record["timing_ms"] or {}, record["cells"] or {}
        mfaalog.info(f"[BagScan] {record['label']}（{'+'.join(record['scope'])}）：读到 {len(record['quantities'])}，"
                     f"判 0 {len(record['absent'])}，未知 {len(record['unknown'])}，结束={record['termination']}，"
                     f"{record['pages']} 页，{round(timing.get('total', 0) / 1000)} 秒"
                     f"（读图 {round(timing.get('read', 0) / 1000, 1)} 秒，点开 {cells.get('opened', 0)} 格，"
                     f"凭图标跳过 {cells.get('by_icon', 0)}，同图跳过 {cells.get('same_picture', 0)}，"
                     f"没核实 {cells.get('unverified', 0)}）")
        if record["unknown"]:
            mfaalog.info(f"[BagScan] 未知（不判 0）—— {unknown_text(record['unknown'])}")
        # 允许按 BagReturn 原路返回；业务是否完整只看记录里的 complete。
        return True


def run_bag_scan(context: Context, *, label: str | None, targets: str | list[str] = "all",
                 scope: str | list[str] = "A", note: str | None = None, origin: dict | None = None) -> dict:
    """同步执行一次仓检（开包 → 按页扫描 → 原路返回），返回扫描记录副本。

    scope：扫描类型，Agt_BagScan_Config 里定义的键，可给一个或一组——A 存货（食材、料理）、
    B 票券与养成材料、C 装备制作材料；一趟扫完它们的并集，记录里的 scope 写明扫了哪些。
    A、C 各自连成一段，走完那一段就结清；带 B 时扫到列表底。
    executed=False 表示没走到扫描核心（账号未确认、配置无效或进包失败），此时没有任何读数；
    returned=False 表示子任务没正常结束，页面位置未知，调用方须自行恢复。
    origin：谁在什么位置触发的这次盘点，原样写进扫描记录；库存记账靠它认出自己的盘点。
    """
    if not sync_from_context(context, where="BagScan"):
        return {"executed": False, "returned": False, "error": "account"}
    if not scope or not all(isinstance(key, str) for key in ([scope] if isinstance(scope, str) else scope)):
        raise ValueError(f"未知扫描类型: {scope}")
    scan_id = uuid4().hex
    param = {"scan_id": scan_id, "label": _label(label), "targets": targets, "scope": scope, "note": note}
    if origin is not None:
        param["origin"] = origin
    local = context.clone()
    detail = local.run_task(ENTRY, {RUN: {"custom_action_param": param}})
    record = get_scan(scan_id)
    returned = bool(detail is not None and detail.status.succeeded)
    if record is None:
        return {"executed": False, "returned": returned, "error": "scan_not_reached", "scan_id": scan_id}
    record.update(executed=True, returned=returned)
    return record


@AgentServer.custom_action("BagScan")
class BagScan(CustomAction):
    """启动器：放进任何流程，参数 {label, scope?, targets?, note?}；跑完开包、扫描、返回这个节点才结束。

    scope 写一个扫描类型或一组（如 "A"、["B", "C"]），默认 "A"。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
            record = run_bag_scan(context, label=params.get("label"), targets=params.get("targets", "all"),
                                  scope=params.get("scope", "A"), note=params.get("note"))
        except Exception as exc:
            mfaalog.error(f"[BagScan] 启动失败：{exc}")
            return False
        if not record["executed"]:
            mfaalog.error(f"[BagScan] 没能执行扫描：{record.get('error')}")
        elif not record["returned"]:
            mfaalog.warning("[BagScan] 扫描后没能正常返回，页面位置未知")
        return bool(record["executed"] and record["returned"])
