"""LootFeedPending：掉落播报里有没有还没收取的东西。

命中 = 上次 LootFeedCollect 之后又用 LootFeedCast 点过技能，或有还没交出去的条目。
给 `[JumpBack]` 挂的收取节点当识别用：收完就不再命中，父节点自然往下走，不会回环。

参数（custom_recognition_param，可省）：
    feed    会话名，与 LootFeedCast / LootFeedCollect 的一致。默认 "default"
"""

import json
from typing import Optional, Union

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition
from maa.define import RectType

from utils import loot_feed, mfaalog


@AgentServer.custom_recognition("LootFeedPending")
class LootFeedPending(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        try:
            raw = argv.custom_recognition_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw).strip() or "{}")
            feed = str((params or {}).get("feed", "default"))
            task_id = getattr(getattr(argv, "task_detail", None), "task_id", 0)
            if not loot_feed.has_pending(task_id, feed):
                return None
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"feed": feed, "pending": True})
        except Exception as e:
            mfaalog.error(f"[LootFeed] LootFeedPending 出错，按没有待收取处理: {e!r}")
            return None
