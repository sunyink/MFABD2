"""Synchronous Context events run before the first business screenshot."""

from maa.agent.agent_server import AgentServer
from maa.context import ContextEventSink
from maa.event_sink import NotificationType
from maa.tasker import TaskerEventSink
from utils import mfaalog

from .guard import StartupGuard
# ADB 由普通 StartGame pipeline 启动，暂不运行公共前台探测与等待。
guard = StartupGuard(mfaalog.info, mfaalog.error, warn=mfaalog.warning)

# MaaFw 5.12.2's context-sink decorator does not initialize its ctypes binding.
# Initialize explicitly instead of depending on unrelated action import order.
AgentServer._set_api_properties()

# MFAA 在两个任务之间投递的占位任务：队列不空，Tasker 就不会在任务边界 inactive（撤销伪最小化）。
# 它不是用户任务，不做启动准备，也不算一个任务结束。
QUEUE_HOLD_NODE = "MFAA_QueueHold"

@AgentServer.context_sink()
class StartupSink(ContextEventSink):
    def on_node_pipeline_node(self, context, noti_type, detail):
        if detail.name == QUEUE_HOLD_NODE:
            return
        if noti_type == NotificationType.Starting:
            # Nested run_task events have a new task id; their cloned Context
            # still identifies the top-level task that owns this preparation.
            guard.ensure(context)


@AgentServer.tasker_sink()
class StartupTaskSink(TaskerEventSink):
    def on_tasker_task(self, tasker, noti_type, detail):
        if detail.entry == QUEUE_HOLD_NODE:
            return
        # 任务结束后框架会自己撤销伪最小化（post_inactive），那是它的收尾，不该再被补回去。
        if noti_type in (NotificationType.Succeeded, NotificationType.Failed):
            guard.task_finished()
