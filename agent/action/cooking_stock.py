"""料理短缺采集：原生判红触发，只记录事实，采购量由后续策略计算。

所有 OCR/ROI、材料模板与判定阈值均由 pipeline 提供，不依赖菜品配方顺序表。
get_cooking_stock(task_id) 返回本任务每道料理的最新观察；complete=False 的
记录不能作为完整库存使用。不同料理的观察有时间差，不能把它们的缺口相加。
结果在本 agent 进程保留最近 16 个任务，同时把观察与逐材料有效数量写进当前账号存档。
"""

import copy
import json
import re
import threading
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone

import numpy as np
from PIL import Image

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_store import (
    save_cooking_stock_observation, get_inventory_items,
    invalidate_inventory_quantities, utc_now,
)
from utils.name_i18n import canon


_OBSERVATIONS = OrderedDict()
_LOCK = threading.Lock()
_SUMMARY_NODE = "Arbitrage_Cooking_StockSummary"
_SCAN_STARTS = OrderedDict()


def get_cooking_scan_start(task_id: int) -> str | None:
    with _LOCK:
        return _SCAN_STARTS.get(task_id)


@AgentServer.custom_action("CookingInventoryBoundary")
class CookingInventoryBoundary(CustomAction):
    """记录本轮起点；制作前只作废当前配方的旧数量，随后执行原有点击。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            if params["mode"] == "begin":
                with _LOCK:
                    _SCAN_STARTS.setdefault(argv.task_detail.task_id, utc_now())
                    _SCAN_STARTS.move_to_end(argv.task_detail.task_id)
                    while len(_SCAN_STARTS) > 16:
                        _SCAN_STARTS.popitem(last=False)
                return True
            if params["mode"] != "commit":
                raise ValueError("未知库存边界模式")
            if not sync_from_context(context, where="CookingInventoryBoundary"):
                return False
            raw_scan = context.get_node_object(params["snapshot_node"]).action.param.custom_action_param
            scan = raw_scan if isinstance(raw_scan, dict) else json.loads(str(raw_scan))
            catalog = context.get_node_object(scan["template_node"]).attach["templates"]
            items = get_inventory_items()
            known = {canon(name) for name in catalog
                     if items.get(canon(name), {}).get("quantity_status") == "known"}
            names = []
            if known:
                try:
                    image = context.tasker.controller.post_screencap().wait().get()
                    if image is None or not image.size:
                        raise RuntimeError("制作前截图失败")
                    recipe_names = _current_recipe_materials(context, scan, image)
                    names = sorted(known.intersection(recipe_names))
                except Exception as exc:
                    # 不能确定配方时仍允许原制作流程继续，但不能保留可能被消耗的旧量。
                    names = sorted(known)
                    mfaalog.warning(f"[CookingStock] 当前配方未核实，全部 {len(names)} 项旧数量待补查: {exc}")
            if not invalidate_inventory_quantities(names, "cooking_attempt"):
                raise RuntimeError("制作前库存作废未能写入")
            if names:
                mfaalog.info("[CookingStock] 制作前待更新: " + "、".join(names))
            result = context.run_action(params["click_node"], box=argv.box)
            return bool(result is not None and result.success)
        except Exception as exc:
            mfaalog.error(f"[CookingStock] 库存边界处理失败: {exc}")
            return False


def get_cooking_stock(task_id: int) -> list[dict]:
    """返回副本；未观察过的任务返回空列表，不补零、不跨任务继承。"""
    with _LOCK:
        return copy.deepcopy(_OBSERVATIONS.get(task_id, []))


def _save(task_id: int, record: dict) -> None:
    with _LOCK:
        records = _OBSERVATIONS.setdefault(task_id, [])
        for index, previous in enumerate(records):
            if record["recipe"] and previous["recipe"] == record["recipe"]:
                records[index] = record
                break
        else:
            records.append(record)
        _OBSERVATIONS.move_to_end(task_id)
        while len(_OBSERVATIONS) > 16:
            _OBSERVATIONS.popitem(last=False)


def _clean(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _read_texts(context: Context, node: str, image, retry: bool = False) -> tuple[list[str], int | None, bool]:
    if retry:
        config = context.get_node_object(node)
        scale = config.attach.get("retry_scale", 1)
        if not isinstance(scale, int) or not 1 < scale <= 4:
            return [], None, False
        x, y, width, height = config.recognition.param.roi
        crop = image[y:y + height, x:x + width]
        # 只缩放通道数值，不交换 BGR 顺序；不保存或改变原截图。
        enlarged = np.asarray(Image.fromarray(crop).resize(
            (width * scale, height * scale), Image.Resampling.BICUBIC,
        ))
        result = context.run_recognition(node, enlarged, {node: {"roi": [0, 0, width * scale, height * scale]}})
    else:
        result = context.run_recognition(node, image)
    if result is None:
        raise RuntimeError(f"OCR 节点未能执行: {node}")
    reco_id = getattr(result, "reco_id", None)
    if not result.hit:
        return [], reco_id, False
    items = getattr(result, "filtered_results", None) or getattr(result, "all_results", None) or []
    texts = []
    for item in items:
        text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        if text:
            texts.append(str(text))
    return texts, reco_id, True


def _match_materials(context: Context, node: str, image, slot_rois: list,
                     recognition_log: list[dict] | None = None) -> list[list[dict]]:
    """逐模板扫描材料栏，按图标中心归到数量槽位；坐标来自 pipeline。"""
    tuning = context.get_node_object(node).attach
    candidates = [[] for _ in slot_rois]
    for name, template in tuning["templates"].items():
        result = context.run_recognition(node, image, {node: {"template": [template]}})
        if result is None:
            raise RuntimeError(f"材料模板识别未能执行: {name}")
        if not result.hit:
            continue
        # 只记命中：一次扫描 60 个模板通常只中 5 个，未命中项对排错没有信息量。
        if recognition_log is not None:
            recognition_log.append({
                "role": "material_template",
                "node": node,
                "candidate_name": canon(name),
                "template": template,
                "reco_id": getattr(result, "reco_id", None),
                "hit": True,
                "image": "snapshot",
            })
        for item in result.filtered_results:
            box = item.box
            x, _, width, _ = (box.x, box.y, box.w, box.h) if hasattr(box, "x") else box
            center = x + width / 2
            for index, (left, _, slot_width, _) in enumerate(slot_rois):
                if left <= center < left + slot_width:
                    candidates[index].append({"name": canon(name), "score": float(item.score),
                                              "reco_id": getattr(result, "reco_id", None)})
    # 同名模板多次命中只算一个候选，防止重复框把第二名挤掉。
    for index, items in enumerate(candidates):
        best_by_name = {}
        for item in items:
            name = item["name"]
            if name not in best_by_name or item["score"] > best_by_name[name]["score"]:
                best_by_name[name] = item
        candidates[index] = sorted(best_by_name.values(), key=lambda item: item["score"], reverse=True)
    return candidates


def _current_recipe_materials(context: Context, params: dict, image) -> list[str]:
    """复用短缺采集的图标与槽位配置；配方未识全时不能据此保留其他旧数量。"""
    page = context.run_recognition(params["page_node"], image)
    if page is None or not page.hit:
        raise RuntimeError("截图不在料理详情页")
    material_nodes, presence_nodes = params["material_nodes"], params["presence_nodes"]
    if not material_nodes or len(material_nodes) != len(presence_nodes):
        raise ValueError("材料 OCR 与槽位检测节点数量不一致")
    rois = [context.get_node_object(node).recognition.param.roi for node in material_nodes]
    template_node = params["template_node"]
    margin = context.get_node_object(template_node).attach["score_margin"]
    candidates = _match_materials(context, template_node, image, rois)
    names = []
    for index, (node, ranked) in enumerate(zip(presence_nodes, candidates)):
        present = context.run_recognition(node, image)
        if present is None:
            raise RuntimeError(f"材料槽位 {index + 1} 检测未能执行")
        if not ranked:
            if present.hit:
                raise RuntimeError(f"材料槽位 {index + 1} 图标未识别")
            continue
        if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < margin:
            raise RuntimeError(f"材料槽位 {index + 1} 图标有歧义")
        names.append(ranked[0]["name"])
    if not names or len(set(names)) != len(names):
        raise RuntimeError("配方材料缺失或重复")
    return names


def _unique_parsed(texts: list[str], pattern: str):
    """只接受完整且唯一的读数；不猜斜杠、不拼接互相独立的 det 结果。"""
    values = set()
    for text in texts:
        match = re.fullmatch(pattern, _clean(text))
        if match:
            values.add(tuple(int(value.replace(",", "")) for value in match.groups()))
    return next(iter(values)) if len(values) == 1 else None


_NUMBER = r"(0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"


def collect_cooking_stock(context: Context, params: dict, image) -> dict:
    """同一张截图读取菜名、份数、各材料；局部失败仍保留其他槽位的读数。"""
    record = {
        "recipe": None,
        "trigger": "quantity_red",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "selected_count": None,
        "materials": [],
        "complete": False,
        "errors": [],
        "raw_recipe": [],
        "raw_quantity": [],
        "recognitions": [],
    }

    def read(role, node, retry=False):
        try:
            texts, reco_id, hit = _read_texts(context, node, image, retry=retry)
            record["recognitions"].append({
                "role": role,
                "node": node,
                "reco_id": reco_id,
                "hit": hit,
                "image": "retry_crop" if retry else "snapshot",
            })
            return texts
        except Exception as exc:
            record["errors"].append(f"{node}: {exc}")
            return []

    material_nodes = params["material_nodes"]
    presence_nodes = params["presence_nodes"]
    if not material_nodes or len(material_nodes) != len(presence_nodes):
        raise ValueError("材料 OCR 与槽位检测节点数量不一致")
    record["raw_recipe"] = read("recipe", params["recipe_node"])
    names = {canon(name) for text in record["raw_recipe"]
             if (name := re.sub(r"[*★☆]+$", "", _clean(text)))}
    if len(names) == 1:
        record["recipe"] = names.pop()
    else:
        record["errors"].append("recipe_unreadable")

    record["raw_quantity"] = read("selected_count", params["quantity_node"])
    quantity = _unique_parsed(record["raw_quantity"], _NUMBER + r"[个個]")
    if quantity and quantity[0] > 0:
        record["selected_count"] = quantity[0]
    else:
        record["errors"].append("quantity_unreadable")

    slot_rois = [context.get_node_object(node).recognition.param.roi for node in material_nodes]
    template_node = params["template_node"]
    margin = context.get_node_object(template_node).attach["score_margin"]
    try:
        candidates = _match_materials(
            context, template_node, image, slot_rois, record["recognitions"])
    except Exception as exc:
        candidates = [[] for _ in material_nodes]
        record["errors"].append(str(exc))
    for index, node in enumerate(material_nodes):
        role = f"slot_{index + 1}"
        raw = read(f"{role}_quantity", node)
        present = context.run_recognition(presence_nodes[index], image)
        record["recognitions"].append({
            "role": f"{role}_presence",
            "node": presence_nodes[index],
            "reco_id": getattr(present, "reco_id", None) if present is not None else None,
            "hit": bool(present is not None and present.hit),
            "image": "snapshot",
        })
        if present is None:
            record["errors"].append(f"slot_{index + 1}:presence_failed")
        ranked = candidates[index]
        if present is not None and not present.hit and not raw and not ranked:
            continue
        pair = _unique_parsed(raw, _NUMBER + "/" + _NUMBER)
        retry_raw = []
        if not pair:
            retry_raw = read(f"{role}_quantity", node, retry=True)
            pair = _unique_parsed(retry_raw, _NUMBER + "/" + _NUMBER)
        name = ranked[0]["name"] if ranked else None
        if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < margin:
            name = None
        material = {
            "slot": index + 1,
            "name": name,
            "stock": pair[0] if pair else None,
            "per_craft": pair[1] if pair and record["selected_count"] == 1 and pair[1] > 0 else None,
            "displayed_required": pair[1] if pair else None,
            "raw": raw,
            "retry_raw": retry_raw,
            "match_candidates": ranked,
            "status": "ok",
        }
        if not pair or pair[1] <= 0:
            material["stock"] = None
            material["status"] = "unreadable"
        elif not name:
            material["status"] = "material_unknown"
        elif record["selected_count"] != 1:
            # 仅一份时验证画面分母。其他数量下不猜分母是否随选择份数变化。
            material["status"] = "quantity_unverified"
        if material["status"] != "ok":
            record["errors"].append(f"slot_{index + 1}:{material['status']}")
        record["materials"].append(material)
    if not record["materials"]:
        record["errors"].append("materials_missing")
    if len({item["name"] for item in record["materials"] if item["name"]}) != sum(
        item["name"] is not None for item in record["materials"]
    ):
        record["errors"].append("duplicate_material")
    record["complete"] = not record["errors"]
    return record

@AgentServer.custom_recognition("CookingStockSummary")
class CookingStockSummary(CustomRecognition):
    """把 custom 最终采用的业务结果写入框架识别 details。"""

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            raw = argv.custom_recognition_param
            payload = raw if isinstance(raw, dict) else json.loads(str(raw))
            if not isinstance(payload, dict):
                raise TypeError("summary payload 必须是对象")
            detail = copy.deepcopy(payload)
            detail["kind"] = "cooking_stock_summary"
        except Exception as exc:
            detail = {
                "kind": "cooking_stock_summary",
                "complete": False,
                "errors": [f"summary_payload_invalid: {exc}"],
            }
        # 这是逻辑观测，不把 complete=False 冒充框架识别失败；完整性看 detail 字段。
        return CustomRecognition.AnalyzeResult(box=(0, 0, 0, 0), detail=detail)


def _summary_line(record: dict) -> str:
    """GUI 只收一行业务摘要；逐次识别明细由汇总识别的 details 承担。"""
    def shown(value):
        return "?" if value is None else value

    items = " ".join(
        f"{shown(material.get('name'))}={shown(material.get('stock'))}"
        f"/{shown(material.get('displayed_required'))}"
        for material in record.get("materials") or []
    )
    head = (f"{shown(record.get('recipe'))} 份数={shown(record.get('selected_count'))} "
            f"complete={record.get('complete')}")
    return f"{head} {items}".rstrip()


def _emit_summary_detail(context: Context, image, record: dict) -> bool:
    """同一张截图上追加一条汇总识别记录，供 maafw.log 与子识别图交叉复盘。"""
    if image is None or not getattr(image, "size", 0):
        return False
    result = context.run_recognition(
        _SUMMARY_NODE,
        image,
        pipeline_override={
            _SUMMARY_NODE: {
                "recognition": "Custom",
                "custom_recognition": "CookingStockSummary",
                "custom_recognition_param": record,
            },
        },
    )
    return bool(result is not None and result.hit)


@AgentServer.custom_action("CookingStockSnapshot")
class CookingStockSnapshot(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        task_id = argv.task_detail.task_id
        if not sync_from_context(context, where="CookingStockSnapshot"):
            return False
        image = None
        guard_recognitions = []
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            image = context.tasker.controller.post_screencap().wait().get()
            if image is None or not image.size:
                raise RuntimeError("料理库存截图失败")
            page = context.run_recognition(params["page_node"], image)
            trigger = context.run_recognition(argv.node_name, image)
            guard_recognitions = [
                {
                    "role": "page_guard", "node": params["page_node"],
                    "reco_id": getattr(page, "reco_id", None) if page is not None else None,
                    "hit": bool(page is not None and page.hit), "image": "snapshot",
                },
                {
                    "role": "trigger_guard", "node": argv.node_name,
                    "reco_id": getattr(trigger, "reco_id", None) if trigger is not None else None,
                    "hit": bool(trigger is not None and trigger.hit), "image": "snapshot",
                },
            ]
            if page is None or not page.hit or trigger is None or not trigger.hit:
                raise RuntimeError("采集截图已不在料理短缺状态")
            record = collect_cooking_stock(context, params, image)
            record["recognitions"] = guard_recognitions + record["recognitions"]
        except Exception as exc:
            # 仍然存下失败观察，不能让后续把采集失败当作没有缺料。
            record = {"recipe": None, "trigger": "quantity_red",
                      "observed_at": datetime.now(timezone.utc).isoformat(),
                      "selected_count": None, "materials": [], "complete": False,
                      "errors": [str(exc)],
                      "recognitions": guard_recognitions}
        record["task_id"] = task_id
        _save(task_id, record)
        try:
            stored = save_cooking_stock_observation(record)
        except Exception as exc:
            stored = False
            mfaalog.error(f"[CookingStock] 库存存档异常: {exc}")
        record["persisted"] = stored
        if not stored:
            mfaalog.warning("[CookingStock] 本次观察仍保留在进程内，但未能写入库存存档")
        try:
            emitted = _emit_summary_detail(context, image, record)
        except Exception as exc:
            emitted = False
            mfaalog.warning(f"[CookingStock] 汇总识别 details 写入异常: {exc}")
        if not emitted and image is not None:
            mfaalog.warning("[CookingStock] 未能生成汇总识别记录；业务存档不受影响")
        if record["complete"]:
            mfaalog.info("[CookingStock] " + _summary_line(record))
        else:
            # 只有失败才把全量 record 打给 GUI；成功时明细去 maafw.log 的汇总识别 details 看。
            mfaalog.warning("[CookingStock] " + json.dumps(record, ensure_ascii=False))
        # True 仅表示采集尝试已记录；完整性看 complete。允许 pipeline 继续返回菜单。
        return True
