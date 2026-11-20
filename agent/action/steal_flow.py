"""Explicit failure reporting for the local stealing pipeline."""

import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from utils import mfaalog


@AgentServer.custom_action("StealFail")
class StealFailAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            raw = argv.custom_action_param
            params = json.loads(raw) if isinstance(raw, str) else raw
            reason = params.get("reason", "未确认偷窃完成") if isinstance(params, dict) else "失败参数无效"
        except (TypeError, ValueError):
            reason = "失败参数无法解析"
        mfaalog.error(f"[偷窃] {reason}；本次任务失败，不写入完成记录。")
        return False
