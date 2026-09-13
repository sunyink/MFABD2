"""Compatibility adapters for existing StartGame pipeline entry names."""

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from startup.common import Budget
from startup.sink import guard
from utils import mfaalog


@AgentServer.custom_action("StartupCheckApp")
class StartupCheckApp(CustomAction):
    def run(self, context, argv):
        result = guard.ensure(context)
        if result is None:
            return False
        kind = context.tasker.controller.info.get("type")
        # A new/resumed game needs the existing Logo/Loading branch.
        # Other platforms retain their controller's StartApp path.
        # PC may have launched in the separate pretask process, so enter the
        # loading branch (which also recognizes an already-ready home screen).
        return kind == "adb" and not result.changed


@AgentServer.custom_action("StartupRunApp")
class StartupRunApp(CustomAction):
    def run(self, context, argv):
        result = guard.ensure(context)
        if result is None:
            return False
        controller = context.tasker.controller
        if controller.info.get("type") in ("adb", "win32", "playcover"):
            return True
        # PlayCover must already be running to connect; StartApp is unsupported.
        # Native Android overrides this node with StartApp in its resource layer.
        try:
            budget = Budget(mfaalog.info, lambda: context.tasker.stopping)
            job = controller.post_start_app("com.neowizgames.game.browndust2")
            while not job.done:
                budget.pause(0.1)
            return job.succeeded
        except Exception as exc:
            mfaalog.error(f"[启动准备] 控制器启动游戏失败：{exc}")
            context.tasker.post_stop()
            return False
