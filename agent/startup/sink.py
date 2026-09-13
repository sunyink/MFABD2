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

# MaaFw 5.12.2's context-sink decorator does not initialize its ctypes binding.
# Initialize explicitly instead of depending on unrelated action import order.
AgentServer._set_api_properties()

@AgentServer.context_sink()
class StartupSink(ContextEventSink):
    def on_node_pipeline_node(self, context, noti_type, detail):
        if noti_type == NotificationType.Starting:
            # Nested run_task events have a new task id; their cloned Context
            # still identifies the top-level task that owns this preparation.
            guard.ensure(context)
