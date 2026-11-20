"""MFAA 在两个任务之间投递的占位任务：队列不空，Tasker 就不会在任务边界 inactive（撤销伪最小化）。

它不是用户任务，任务事件回调里一律跳过。自家客户端 v2.16.5 系列用固定名 MFAA_QueueHold；
上游 v2.16.6 起改成每次启动不同的名字 MFAA_QueueHold_<随机串>。两种都认，新旧客户端混用不出错。
"""

LEGACY_NAME = "MFAA_QueueHold"
NAME_PREFIX = "MFAA_QueueHold_"


def is_queue_hold(name) -> bool:
    """节点名或任务入口是不是 MFAA 的占位任务。"""
    return isinstance(name, str) and (name == LEGACY_NAME or name.startswith(NAME_PREFIX))
