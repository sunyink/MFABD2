"""Early account check for StartGame; the UI configuration task is a separate no-op."""
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer
from utils.account_sync import sync_from_context


@AgentServer.custom_action("SwitchAccountCheckpoint")
class SwitchAccountCheckpointAction(CustomAction):
    def run(self, context, argv):
        return sync_from_context(context, where="SwitchAccountCheckpoint")
