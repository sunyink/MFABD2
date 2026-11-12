"""通用背包仓检：启动器 BagScan、扫描核心 BagScanRun，以及供 py 主控调用的 run_bag_scan。

pipeline 侧在 assets/resource/*/pipeline/BagScan.json：开包、复位、排序、点开/关闭详情、
翻页、原路返回都是节点；这里只决定点哪几个格子、读到的数怎么记、什么时候能判 0。
设计见 priv doc/agent/bag/背包仓检_设计.md。
"""

from collections import OrderedDict
import copy
from functools import lru_cache
import json
from pathlib import Path
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.bag_scan_engine import BagScanEngine, Reading
from utils.inventory_store import save_scan_record, set_inventory_quantities, utc_now
from utils.name_i18n import canon
from utils.ocr_item_name import clean, item_aliases
from utils.ocr_read import read_texts


__all__ = ["BagScan", "BagScanRun", "get_scan", "run_bag_scan"]

ENTRY = "BagScan_Entry"
RUN = "BagScan_Run"
OPEN, READY = "BagScan_Detail_Open", "BagScan_Detail_Ready"
CLOSE, CLOSED = "BagScan_Detail_Close", "BagScan_Detail_Closed"
PAGE, MOVED, BOTTOM = "BagScan_Page_Next", "BagScan_Page_Moved", "BagScan_Page_Bottom"
SWIPE = "Agt_BagScan_Page_Swipe"
RESET, RESET_EXIT = "BagScan_TabReset", "BagScan_TabReset_Egress"
NAME_OCR, QUANTITY_OCR = "Rec_BagScan_Name_Ocr", "Rec_BagScan_Quantity_Ocr"
# 档案键 → 档案节点。ingredients + dishes 是存货，growth 是货币票券与养成材料，craft 是装备制作材料（设计 §14）。
PROFILES = {"ingredients": "Agt_BagScan_Profile_Ingredients", "dishes": "Agt_BagScan_Profile_Dishes",
            "growth": "Agt_BagScan_Profile_Growth", "craft": "Agt_BagScan_Profile_Craft"}
# 末端（技能书、史莱姆）之前的那一段：末端标志点开读出来是这些档案的物品，就不算到了末端。
SECTION_PROFILES = ("ingredients", "dishes")

_SCANS = OrderedDict()


@lru_cache(maxsize=1)
def _bag_items():
    """tools/gen_bag_templates.py 按物品表生成的目录物品名：[{cn, tw, alias, profile, kind}]。"""
    path = Path(__file__).resolve().parents[1] / "data" / "bag_item_names.json"
    return tuple(json.loads(path.read_text(encoding="utf-8"))["items"])


def _profile_names(profile):
    return frozenset(item["cn"] for item in _bag_items() if item["profile"] == profile)


@lru_cache(maxsize=1)
def _aliases():
    """读名用的名录：共用名录（食材等，含简繁异名）加上背包目录物品的简繁名。

    共用名录也服务商店 OCR，不往里加东西；这里另起一份，只给背包仓检用。同一个写法指向两个物品的不收。
    """
    aliases = dict(item_aliases())
    seen = {}
    for item in _bag_items():
        # alias：游戏物品表里与项目规范名不同的简体原名（如「光圣石」→ 规范名「光明圣石」）
        for text in {item["cn"], item.get("tw") or "", *item.get("alias", [])} - {""}:
            seen.setdefault(clean(text), set()).add(item["cn"])
    for text, names in seen.items():
        if len(names) == 1 and aliases.get(text, next(iter(names))) == next(iter(names)):
            aliases[text] = next(iter(names))
    return aliases


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
    """把扫描核心要用的动作落到 BagScan.json 的节点上；坐标与轮询参数都在节点里，可按资源包覆盖。"""

    def __init__(self, context: Context, profile: dict):
        self.context = context
        self.item_node = profile["item_node"]
        self.color_node = profile.get("color_node")
        self.color_sub = profile.get("color_sub")
        self.ready_node = profile["ready_node"]
        self.end_node = profile.get("end_node") or None

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

    def match(self, entry, image):
        """目录项（模板路径，或 {template, green_mask, threshold, color}）在本页命中的全部格子 [(框, 分数)]。

        生成的模板透明处涂成纯绿，条目里带 green_mask: true；手工截图是路径字符串，沿用节点自己的设置。
        """
        spec: dict = {"template": entry} if isinstance(entry, str) else dict(entry)
        item: dict = {"template": [spec["template"]], "green_mask": bool(spec.get("green_mask", False))}
        if spec.get("threshold") is not None:
            item["threshold"] = spec["threshold"]
        color: dict = spec.get("color") or {}
        if not color:
            result = self._recognize(self.item_node, image, {self.item_node: item})
            if not result.hit:
                return []
            matches = getattr(result, "filtered_results", None) or []
            return [(_box(match.box), float(match.score)) for match in matches] or [(_box(result.box), 1.0)]
        if not (self.color_node and self.color_sub):
            raise RuntimeError("目录项带颜色核，但档案没配 color_node / color_sub")
        result = self._recognize(self.color_node, image, {
            self.item_node: item,
            self.color_sub: {"lower": color["lower"], "upper": color["upper"], "count": color["count"]}})
        if not result.hit:
            return []
        # And 对外只留一个框；通过颜色核的全部格子在颜色子识别的 filtered 里（设计 §14.4），一个都不能漏。
        best = getattr(result, "best_result", None)
        subs = {getattr(sub, "name", ""): sub for sub in getattr(best, "sub_results", None) or []}
        passed = getattr(subs.get(self.color_sub), "filtered_results", None) or []
        return [(_box(match.box), 1.0) for match in passed] or [(_box(result.box), 1.0)]

    def end_markers(self, image):
        if not self.end_node:
            return []
        result = self._recognize(self.end_node, image)
        if not result.hit:
            return []
        return [_box(item.box) for item in getattr(result, "filtered_results", None) or []] or [_box(result.box)]

    def open_detail(self, box):
        detail = self.context.run_task(OPEN, {OPEN: {"target": list(box)}})
        ready = _ran(detail, READY)
        return _reading(ready.recognition) if ready is not None and ready.recognition is not None else None

    def read_detail(self):
        result = self._recognize(READY, self.capture())
        return _reading(result) if result.hit else None

    def read_names(self):
        result = self._recognize(NAME_OCR, self.capture())
        return [(str(getattr(item, "text", "")), float(getattr(item, "score", 0))) for item in _ocr_items(result)]

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


def _targets(raw, catalog, names):
    if raw in (None, "", "all"):
        # 按完整名录展开：资源包缺图的也列进来，由扫描核心记为 template_missing，不悄悄少扫。
        return list(catalog) + sorted(set(names) - catalog.keys())
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ValueError("targets 只能是 all 或名称列表")
    return list(dict.fromkeys(canon(name.strip()) for name in raw if name.strip()))


def _profile_nodes(raw):
    """BagScan_Run 的 profile 参数（一个档案节点名，或一组）→ 去重后的节点名列表。"""
    nodes = [raw] if isinstance(raw, str) else list(raw or [])
    nodes = list(dict.fromkeys(nodes))
    known = set(PROFILES.values())
    if not nodes or not all(isinstance(node, str) and node in known for node in nodes):
        raise ValueError(f"未知仓检档案: {raw}")
    return nodes


def _merge_profiles(context, nodes):
    """几个档案一起扫：目录与名单取并集；识别节点、复核参数取第一个；页数与时限取最大。

    只有全部档案都在同一个末端之前（都配了同一个 end_node）才允许到末端提前收尾，
    否则一直扫到列表底，判 0 的证据记 list_bottom（设计 §14.2）。
    """
    keys = {node: key for key, node in PROFILES.items()}
    profiles = [context.get_node_object(node).attach for node in nodes]
    catalog, names = {}, set()
    for node, profile in zip(nodes, profiles):
        catalog.update(context.get_node_object(profile["catalog_node"]).attach["templates"])
        names |= _profile_names(keys[node])
    end_nodes = {profile.get("end_node") or "" for profile in profiles}
    section_end = len(end_nodes) == 1 and "" not in end_nodes
    merged = dict(profiles[0])
    merged.update(end_node=next(iter(end_nodes)) if section_end else "",
                  zero_evidence=profiles[0]["zero_evidence"] if section_end else "list_bottom",
                  max_pages=max(profile["max_pages"] for profile in profiles),
                  timeout_seconds=max(profile["timeout_seconds"] for profile in profiles))
    return merged, catalog, names, section_end, [keys[node] for node in nodes]


def _label(raw):
    label = str(raw or "").strip()
    if not 1 <= len(label) <= 40:
        raise ValueError("label 必须是 1～40 字的扫描用途")
    return label


def _log(level, text):
    getattr(mfaalog, level)(f"[BagScan] {text}")


_UNKNOWN_TEXT = {
    "template_missing": "缺模板图", "detail_unreadable": "详情未核实", "continuity_broken": "翻页接不上",
    "persist_failed": "存档写入失败", "page_limit": "翻页上限", "timeout": "超时", "stopped": "任务停止",
    "state_lost": "列表状态丢失", "error": "扫描出错", "not_scanned": "没进背包核对",
}


def unknown_text(unknown):
    """{名称: 原因码} → 按原因分组的一行说明，供日志列出未知物品。"""
    groups = {}
    for name, reason in unknown.items():
        groups.setdefault(reason, []).append(name)
    return "；".join(f"{_UNKNOWN_TEXT.get(reason, reason)}：{'、'.join(names)}" for reason, names in groups.items())


def _save_absent(record, reference, evidence):
    """判 0 写进存档才算数；没写进去的改记未知，扫描随之不完整。"""
    names = record["absent"]
    saved = False
    try:
        saved = set_inventory_quantities(dict.fromkeys(names, 0), "bag_absent", {**reference, "evidence": evidence})
    finally:
        if not saved:
            record["absent"] = []
            record["unknown"] = {**record["unknown"], **dict.fromkeys(names, "persist_failed")}
    if not saved:
        raise RuntimeError("判 0 写入失败")


@AgentServer.custom_action("BagScanRun")
class BagScanRun(CustomAction):
    """扫描核心：BagScan_Run 节点。只观察、不作废任何旧量；完整与否只看记录里的 complete。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        raw = argv.custom_action_param
        params = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
        record = {"scan_id": params.get("scan_id") or uuid4().hex, "label": str(params.get("label") or "未指定"),
                  "note": params.get("note"), "started_at": utc_now(), "job_id": None,
                  "profile": params.get("profile"), "scope": [], "target_count": 0, "complete": False,
                  "termination": "error", "continuity": "broken", "pages": 0,
                  "quantities": {}, "absent": [], "unknown": {}, "timing_ms": {}}
        if not sync_from_context(context, where="BagScanRun"):
            return False
        try:
            record["job_id"] = context.get_task_job().job_id
            record["label"] = _label(record["label"])
            profile, catalog, names, section_end, record["scope"] = _merge_profiles(
                context, _profile_nodes(params.get("profile")))
            targets = _targets(params.get("targets"), catalog, names)
            record["target_count"] = len(targets)
            item = context.get_node_object(profile["item_node"])
            reference = {"scan_id": record["scan_id"], "label": record["label"]}

            def persist(page, quantities):
                if not set_inventory_quantities(quantities, "bag_detail", {**reference, "page": page}):
                    raise RuntimeError("库存写入失败")

            engine = BagScanEngine(
                MaaBagDevice(context, profile), catalog, targets,
                grid_roi=item.recognition.param.roi, row_height=item.attach["row_height"],
                footer_rows=item.attach.get("footer_rows", 0),
                section_names=frozenset().union(*(_profile_names(key) for key in SECTION_PROFILES)),
                section_end=section_end, aliases=_aliases(),
                max_pages=profile["max_pages"], timeout_seconds=profile["timeout_seconds"],
                confirm_frames=profile["confirm_frames"], box_tolerance=profile["box_tolerance"],
                continuity_threshold=profile["continuity_threshold"],
                zero_evidence=profile["zero_evidence"], on_page=persist, log=_log)
            result = engine.run()
            record.update({key: result[key] for key in (
                "complete", "termination", "continuity", "pages", "quantities", "absent", "unknown", "timing_ms")})
            if result["error"]:
                record["error"] = result["error"]
            if record["absent"]:
                _save_absent(record, reference, result["zero_evidence"])
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
        seconds = round((record["timing_ms"] or {}).get("total", 0) / 1000)
        mfaalog.info(f"[BagScan] {record['label']}：读到 {len(record['quantities'])}，判 0 {len(record['absent'])}，"
                     f"未知 {len(record['unknown'])}，结束={record['termination']}，{record['pages']} 页，{seconds} 秒")
        if record["unknown"]:
            mfaalog.info(f"[BagScan] 未知（不判 0）—— {unknown_text(record['unknown'])}")
        # 允许按 BagReturn 原路返回；业务是否完整只看记录里的 complete。
        return True


def run_bag_scan(context: Context, *, label: str | None, targets: str | list[str] = "all",
                 profile: str | list[str] = "ingredients", note: str | None = None) -> dict:
    """同步执行一次仓检（开包 → 按页扫描 → 原路返回），返回扫描记录副本。

    profile：档案键（ingredients / dishes / growth / craft），或一组键——一趟扫完它们的并集，
    记录里的 scope 写明扫了哪些。含 growth、craft 时扫到列表底才判 0。
    executed=False 表示没走到扫描核心（账号未确认、配置无效或进包失败），此时没有任何读数；
    returned=False 表示子任务没正常结束，页面位置未知，调用方须自行恢复。
    """
    if not sync_from_context(context, where="BagScan"):
        return {"executed": False, "returned": False, "error": "account"}
    keys = [profile] if isinstance(profile, str) else list(profile)
    if not keys or any(key not in PROFILES for key in keys):
        raise ValueError(f"未知仓检档案: {profile}")
    scan_id = uuid4().hex
    nodes = [PROFILES[key] for key in dict.fromkeys(keys)]
    param = {"scan_id": scan_id, "label": _label(label), "targets": targets,
             "profile": nodes[0] if len(nodes) == 1 else nodes, "note": note}
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
    """启动器：放进任何流程，参数 {label, targets?, profile?, note?}；跑完开包、扫描、返回这个节点才结束。

    profile 可写一个档案键或一组（如 ["growth", "craft"]），默认 ingredients。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
            record = run_bag_scan(context, label=params.get("label"), targets=params.get("targets", "all"),
                                  profile=params.get("profile", "ingredients"), note=params.get("note"))
        except Exception as exc:
            mfaalog.error(f"[BagScan] 启动失败：{exc}")
            return False
        if not record["executed"]:
            mfaalog.error(f"[BagScan] 没能执行扫描：{record.get('error')}")
        elif not record["returned"]:
            mfaalog.warning("[BagScan] 扫描后没能正常返回，页面位置未知")
        return bool(record["executed"] and record["returned"])
