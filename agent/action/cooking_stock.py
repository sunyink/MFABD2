"""料理短缺采集：原生判红触发，只记录事实，采购量由后续策略计算。

所有 OCR/ROI、材料模板与判定阈值均由 pipeline 提供，不依赖菜品配方顺序表。
get_cooking_stock(task_id) 返回本任务每道料理的最新观察；complete=False 的
记录不能作为完整库存使用。不同料理的观察有时间差，不能把它们的缺口相加。
结果只在本 agent 进程保留最近 16 个任务，同时将完整记录写入日志。
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

from utils import mfaalog
from utils.name_i18n import canon


_OBSERVATIONS = OrderedDict()
_LOCK = threading.Lock()


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


def _read_texts(context: Context, node: str, image, retry: bool = False) -> list[str]:
    if retry:
        config = context.get_node_object(node)
        scale = config.attach.get("retry_scale", 1)
        if not isinstance(scale, int) or not 1 < scale <= 4:
            return []
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
    if not result.hit:
        return []
    items = getattr(result, "filtered_results", None) or getattr(result, "all_results", None) or []
    texts = []
    for item in items:
        text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
        if text:
            texts.append(str(text))
    return texts


def _match_materials(context: Context, node: str, image, slot_rois: list) -> list[list[dict]]:
    """逐模板扫描材料栏，按图标中心归到数量槽位；坐标来自 pipeline。"""
    tuning = context.get_node_object(node).attach
    candidates = [[] for _ in slot_rois]
    for name, template in tuning["templates"].items():
        result = context.run_recognition(node, image, {node: {"template": [template]}})
        if result is None:
            raise RuntimeError(f"材料模板识别未能执行: {name}")
        if not result.hit:
            continue
        for item in result.filtered_results:
            box = item.box
            x, _, width, _ = (box.x, box.y, box.w, box.h) if hasattr(box, "x") else box
            center = x + width / 2
            for index, (left, _, slot_width, _) in enumerate(slot_rois):
                if left <= center < left + slot_width:
                    candidates[index].append({"name": canon(name), "score": float(item.score)})
    # 同名模板多次命中只算一个候选，防止重复框把第二名挤掉。
    for index, items in enumerate(candidates):
        best_by_name = {}
        for item in items:
            name = item["name"]
            if name not in best_by_name or item["score"] > best_by_name[name]["score"]:
                best_by_name[name] = item
        candidates[index] = sorted(best_by_name.values(), key=lambda item: item["score"], reverse=True)
    return candidates


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
    }

    def read(node, retry=False):
        try:
            return _read_texts(context, node, image, retry=retry)
        except Exception as exc:
            record["errors"].append(f"{node}: {exc}")
            return []

    material_nodes = params["material_nodes"]
    presence_nodes = params["presence_nodes"]
    if not material_nodes or len(material_nodes) != len(presence_nodes):
        raise ValueError("材料 OCR 与槽位检测节点数量不一致")
    record["raw_recipe"] = read(params["recipe_node"])
    names = {canon(name) for text in record["raw_recipe"]
             if (name := re.sub(r"[*★☆]+$", "", _clean(text)))}
    if len(names) == 1:
        record["recipe"] = names.pop()
    else:
        record["errors"].append("recipe_unreadable")

    record["raw_quantity"] = read(params["quantity_node"])
    quantity = _unique_parsed(record["raw_quantity"], _NUMBER + r"[个個]")
    if quantity and quantity[0] > 0:
        record["selected_count"] = quantity[0]
    else:
        record["errors"].append("quantity_unreadable")

    slot_rois = [context.get_node_object(node).recognition.param.roi for node in material_nodes]
    template_node = params["template_node"]
    margin = context.get_node_object(template_node).attach["score_margin"]
    try:
        candidates = _match_materials(context, template_node, image, slot_rois)
    except Exception as exc:
        candidates = [[] for _ in material_nodes]
        record["errors"].append(str(exc))
    for index, node in enumerate(material_nodes):
        raw = read(node)
        present = context.run_recognition(presence_nodes[index], image)
        if present is None:
            record["errors"].append(f"slot_{index + 1}:presence_failed")
        ranked = candidates[index]
        if present is not None and not present.hit and not raw and not ranked:
            continue
        pair = _unique_parsed(raw, _NUMBER + "/" + _NUMBER)
        retry_raw = []
        if not pair:
            retry_raw = read(node, retry=True)
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


@AgentServer.custom_action("CookingStockSnapshot")
class CookingStockSnapshot(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        task_id = argv.task_detail.task_id
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            image = context.tasker.controller.post_screencap().wait().get()
            if image is None or not image.size:
                raise RuntimeError("料理库存截图失败")
            page = context.run_recognition(params["page_node"], image)
            trigger = context.run_recognition(argv.node_name, image)
            if page is None or not page.hit or trigger is None or not trigger.hit:
                raise RuntimeError("采集截图已不在料理短缺状态")
            record = collect_cooking_stock(context, params, image)
        except Exception as exc:
            # 仍然存下失败观察，不能让后续把采集失败当作没有缺料。
            record = {"recipe": None, "trigger": "quantity_red",
                      "observed_at": datetime.now(timezone.utc).isoformat(),
                      "selected_count": None, "materials": [], "complete": False,
                      "errors": [str(exc)]}
        record["task_id"] = task_id
        _save(task_id, record)
        log = mfaalog.info if record["complete"] else mfaalog.warning
        log("[CookingStock] " + json.dumps(record, ensure_ascii=False))
        # True 仅表示采集尝试已记录；完整性看 complete。允许 pipeline 继续返回菜单。
        return True
