"""Pipeline adapter for confidence selection over an existing native OCR node.

V1 usage::

    "Rec_TargetBest": {
        "recognition": "Custom",
        "custom_recognition": "OCRBestScore",
        "custom_recognition_param": {"node": "Rec_TargetOcr"}
    }

The source node owns ROI, expected, replace and threshold. This adapter uses
argv.image unchanged, runs OCR once and returns the highest-scoring filtered
candidate. Configure source OCR parameters, not an ROI on this adapter.
"""

import json
import re

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition

from utils import mfaalog
from utils.ocr_score import select_best_ocr
from utils.name_i18n import canon
from utils.ocr_item_name import clean, resolve_item_ocr, is_category_label
from utils.sale_name_slots import plan_slots, slot_config


@AgentServer.custom_recognition("OCRBestScore")
class OCRBestScore(CustomRecognition):
    def analyze(self, context, argv):
        source = None
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            source = params.get("node")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("OCRBestScore 需要非空 node 参数")
            definition = context.get_node_data(source)
            recognition = definition.get("recognition") if isinstance(definition, dict) else None
            kind = recognition.get("type") if isinstance(recognition, dict) else recognition
            if kind != "OCR":
                raise ValueError(f"OCRBestScore 源节点必须是原生 OCR: {source}")
            result = context.run_recognition(source, argv.image)
            if result is None:
                return self.AnalyzeResult(box=None, detail={"source": source, "reason": "recognition_unavailable"})
            candidates = (getattr(result, "filtered_results", None) or []) if result.hit else []
            best = select_best_ocr(candidates)
            return self.AnalyzeResult(
                box=best["box"] if best else None,
                detail={"source": source, "source_reco_id": result.reco_id,
                        "candidate_count": len(candidates), "best": best,
                        "reason": "highest_score" if best else "no_filtered_candidate"},
            )
        except Exception as exc:
            mfaalog.error(f"[OCRBestScore] {exc}")
            return self.AnalyzeResult(box=None, detail={"source": source, "reason": str(exc)})


def _name_text(text):
    """Whether a list text may be an item name rather than a quantity, price or heading."""
    text = clean(text)
    return bool(re.search(r"[一-龥]", text)) and not (
        re.fullmatch(r"(?:可[购購][买買]|[拥擁]有|持有|剩[下余餘]|[还還]剩)?[\d.,，．]+[个個]?", text)
        or is_category_label(text))


@AgentServer.custom_recognition("OCRItemName")
class OCRItemName(CustomRecognition):
    """Resolve shop item names before filtering by target, then rank confidence.

    With ``name_slots`` (sale search only), cards are anchored by their type tags and
    only each card's name box is read; badges, ratings and prices elsewhere in the
    list can no longer block zero inventory. Without it the whole list is read.
    """

    def analyze(self, context, argv):
        source = None
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            source, target = params["node"], canon(params["item_name"])
            if not isinstance(source, str) or not source or not target:
                raise ValueError("商品名称识别缺少源节点或目标名称")
            definition = context.get_node_data(source)
            recognition = definition.get("recognition") if isinstance(definition, dict) else None
            if (recognition.get("type") if isinstance(recognition, dict) else recognition) != "OCR":
                raise ValueError(f"商品名称源节点必须是 OCR: {source}")
            result = context.run_recognition(source, argv.image, {source: {"expected": []}})
            if result is None:
                raise ValueError("商品名称 OCR 未执行")
            candidates, unknown, observations, texts = [], [], [], []
            for match in getattr(result, "filtered_results", None) or []:
                candidate = select_best_ocr([match])
                if candidate is None:
                    unknown.append({"basis": "invalid_candidate"})
                else:
                    texts.append(candidate)
            layout = {}
            if params.get("name_slots"):
                reads, layout = self._slot_reads(context, argv.image, source, recognition, texts,
                                                 params["name_slots"], unknown)
            else:
                # The list ROI also contains quantities, prices and category labels.
                reads = [(source, text) for text in texts if _name_text(text["text"])]
            for node, candidate in reads:
                resolved = resolve_item_ocr(context, node, argv.image, candidate)
                observations.append(resolved)
                if not resolved["confirmed"]:
                    unknown.append(resolved)
                elif resolved["name"] == target:
                    candidates.append(candidate)
            best = select_best_ocr(candidates)
            return self.AnalyzeResult(box=best["box"] if best else None, detail={
                "source": source, "source_reco_id": result.reco_id, "item_name": target,
                "candidate_count": len(candidates), "best": best, "names": observations,
                "unconfirmed_names": unknown, "name_read_failed": not observations,
                "reason": "highest_score" if best else "no_confirmed_item", **layout})
        except Exception as exc:
            mfaalog.warning(f"[OCRItemName] {exc}")
            return self.AnalyzeResult(box=None, detail={
                "source": source, "name_read_failed": True, "reason": str(exc)})

    @staticmethod
    def _slot_reads(context, image, source, recognition, texts, config_node, unknown):
        cfg = slot_config(getattr(context.get_node_object(config_node), "attach", None))
        roi = (recognition.get("param") or {}).get("roi")
        if not isinstance(roi, list) or len(roi) != 4:
            raise ValueError(f"商品名称源节点缺少列表 ROI: {source}")
        plan = plan_slots(texts, roi, cfg)
        node, reads = cfg["name_node"], []
        for slot in plan["slots"]:
            result = context.run_recognition(node, image, {node: {"roi": slot["roi"], "expected": []}})
            if result is None:
                raise ValueError("出售名字框 OCR 未执行")
            found = [read for read in (select_best_ocr([match]) for match in
                                       getattr(result, "filtered_results", None) or [])
                     if read is not None and _name_text(read["text"])]
            if not found:
                # A tagged card without a readable name might be the target.
                unknown.append({"basis": "empty_name_slot", "slot": slot["roi"], "tag": slot["tag"]["text"]})
            reads += [(node, read) for read in found]
        extra = [text for text in plan["extra"] if _name_text(text["text"])]
        return reads + [(source, text) for text in extra], {
            "mode": "name_slot", "tag_count": len(plan["tags"]), "top_cut": plan["top_cut"],
            "extra_count": len(extra)}
