"""按节点名清除当前任务中的 Pipeline 命中计数。

用途：
    ``max_hit`` 限制的是节点在一次顶层任务中的最大命中次数。该次数达到
    上限后，节点会被跳过。本动作把指定节点已经累计的命中次数归零，使其
    能在同一任务中重新参与识别；不会修改节点本身的 ``max_hit`` 配置。

行为：
    1. ``node_name`` 支持单个节点名或节点名列表。
    2. 列表按传入顺序逐项处理，每项分别记录节点名、清除前计数与结果。
    3. 节点名按原字符串精确匹配，不解析 ``[Anchor]`` 等节点属性。
    4. 参数错误、节点不存在或框架调用失败都只记日志；动作始终返回
       ``True``，不阻断后续 Pipeline。

Pipeline V1 单节点示例：

    "Task_ClearRetryHitCount": {
        "desc": "清除重试节点已经消耗的 max_hit 次数，然后继续后续流程",
        "rate_limit": 0,
        "pre_delay": 0,
        "action": "Custom",
        "custom_action": "ClearNodeHitCount",
        "custom_action_param": {
            "node_name": "Task_Retry"
        },
        "post_delay": 0,
        "next": [
            "Task_Continue"
        ]
    }

Pipeline V1 多节点参数：

    "custom_action_param": {
        "node_name": [
            "Task_A",
            "Task_B",
            "Task_C"
        ]
    }
"""

import json
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import mfaalog


@AgentServer.custom_action("ClearNodeHitCount")
class ClearNodeHitCountAction(CustomAction):
    """清除一个或多个节点的命中计数，失败只记录日志。

    ``node_name`` 接受单个字符串或字符串列表。节点名按原值精确匹配；
    每个节点依次处理并分别记录结果，动作始终返回成功以继续 Pipeline。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw_param = argv.custom_action_param
            params = raw_param if isinstance(raw_param, dict) else json.loads(str(raw_param))
        except Exception as e:
            mfaalog.error(f"[ClearNodeHitCount] 参数解析失败: {e}")
            return True

        if not isinstance(params, dict):
            mfaalog.error("[ClearNodeHitCount] 参数无效: 必须传入包含 node_name 的对象")
            return True

        raw_names = params.get("node_name")
        names: list[Any]
        if isinstance(raw_names, list):
            if not raw_names:
                mfaalog.error("[ClearNodeHitCount] node_name: 失败（列表不能为空）")
                return True
            names = raw_names
        else:
            names = [raw_names]

        for node_name in names:
            self._clear_one(context, node_name)

        return True

    @staticmethod
    def _clear_one(context: Context, node_name: Any) -> None:
        if not isinstance(node_name, str) or not node_name:
            mfaalog.error(f"[ClearNodeHitCount] {node_name!r}: 失败（节点名必须是非空字符串）")
            return

        try:
            if context.get_node_data(node_name) is None:
                mfaalog.error(f"[ClearNodeHitCount] {node_name}: 失败（节点不存在）")
                return

            before = context.get_hit_count(node_name)
            if not context.clear_hit_count(node_name):
                mfaalog.error(f"[ClearNodeHitCount] {node_name}: 失败（框架未能清除计数）")
                return

            after = context.get_hit_count(node_name)
            if after != 0:
                mfaalog.error(f"[ClearNodeHitCount] {node_name}: 失败（清除后计数为 {after}）")
                return

            mfaalog.info(f"[ClearNodeHitCount] {node_name}: 成功（{before} -> 0）")
        except Exception as e:
            mfaalog.error(f"[ClearNodeHitCount] {node_name}: 失败（{e}）")
