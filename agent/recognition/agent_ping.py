"""AgentPing：恒命中的通信探针，只给带 inverse 的 Env_AgentLost_Stop 用。

通道断开时客户端根本调不到这里，按不中返回，inverse 翻成命中后停止任务。
所以这里不能做任何可能失败的事：抛异常或返回 None 都会被当成「通道断了」。
"""

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition

# 与框架 inverse 命中时的空 Rect 一致，只表示「Agent 应答了」。
HIT_BOX = (0, 0, 0, 0)


@AgentServer.custom_recognition("AgentPing")
class AgentPing(CustomRecognition):
    def analyze(self, context, argv):
        return CustomRecognition.AnalyzeResult(box=HIT_BOX, detail={"result": "pong"})
