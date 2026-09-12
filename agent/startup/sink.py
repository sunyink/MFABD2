"""Synchronous Context events run before the first business screenshot."""

from maa.agent.agent_server import AgentServer
from maa.context import ContextEventSink
from maa.event_sink import NotificationType
from utils import mfaalog

from .guard import StartupGuard
from . import adb
from .maa_compat import ensure_shell_bindings


def prepare_adb(controller, budget):
    ensure_shell_bindings()
    return adb.prepare(controller, budget)


guard = StartupGuard(mfaalog.info, mfaalog.error, adb_prepare=prepare_adb)


@AgentServer.context_sink()
class StartupSink(ContextEventSink):
    def on_node_pipeline_node(self, context, noti_type, detail):
        if noti_type == NotificationType.Starting:
            guard.ensure(context, detail.task_id)
