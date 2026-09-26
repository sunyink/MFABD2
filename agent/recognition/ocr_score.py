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
from utils.ocr_item_name import clean, resolve_item_ocr


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


@AgentServer.custom_recognition("OCRItemName")
class OCRItemName(CustomRecognition):
    """Resolve shop item names before filtering by target, then rank confidence."""

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
            candidates, unknown, observations = [], [], []
            items = getattr(result, "filtered_results", None) or []
            for match in items:
                candidate = select_best_ocr([match])
                if candidate is None:
                    unknown.append({"basis": "invalid_candidate"})
                    continue
                text = clean(candidate["text"])
                # The list ROI also contains quantities, prices and category labels.
                if (not re.search(r"[一-龥]", text)
                        or re.fullmatch(r"(?:可[购購][买買]|[拥擁]有|持有|剩[下余餘]|[还還]剩)?[\d.,，．]+[个個]?", text)
                        or text in {"食物", "食材", "材料", "料理", "料理食材", "售罄", "已售完",
                                    "装备材料", "裝備材料", "装備材料", "裝备材料", "圣石", "聖石"}):
                    continue
                resolved = resolve_item_ocr(context, source, argv.image, candidate)
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
                "reason": "highest_score" if best else "no_confirmed_item"})
        except Exception as exc:
            mfaalog.warning(f"[OCRItemName] {exc}")
            return self.AnalyzeResult(box=None, detail={
                "source": source, "name_read_failed": True, "reason": str(exc)})
