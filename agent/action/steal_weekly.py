"""Maa adapter for the weekly stealing scheduler; account storage stays shared."""

import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from utils import mfaalog
from utils.account_sync import sync_from_context

from .cartridge_lib import manager
from .steal_schedule import WeeklyRunner, completion_params, load_registry, log_report, select_routes


@AgentServer.custom_action("StealWeekly")
class StealWeeklyAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            raw = argv.custom_action_param
            params = json.loads(raw) if isinstance(raw, str) else raw
            routes = select_routes(params, load_registry())
            if context.tasker.stopping:
                mfaalog.info("[偷窃周常] 任务已停止，没有启动路线。")
                return False
            if not sync_from_context(context, where="StealWeekly"):
                mfaalog.error("[偷窃周常] 账号存档未绑定，未执行任何路线。")
                return False

            def completed(route):
                if not sync_from_context(context, where="StealWeekly/check"):
                    raise RuntimeError("账号存档绑定失效")
                return manager.check_availability(completion_params(route))

            report = WeeklyRunner(context, completed, mfaalog).run(
                routes, skip_completed=params.get("skip_completed", True))
            log_report(report, mfaalog)
            return report.succeeded
        except Exception as exc:
            mfaalog.error(f"[偷窃周常] 调度失败：{exc}；任务未完成。")
            return False
