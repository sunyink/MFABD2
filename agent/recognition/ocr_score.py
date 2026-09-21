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

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition

from utils import mfaalog
from utils.ocr_score import select_best_ocr


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
