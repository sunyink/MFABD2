"""每个任务开始时读取一次 MFAA 调试配置，运行期间不轮询。"""

import json
from pathlib import Path

from maa.event_sink import NotificationType
from maa.tasker import TaskerEventSink

from . import mfaalog


class LogEventSink(TaskerEventSink):
    def __init__(self, config_path: Path):
        self.config_path = config_path

    def on_tasker_task(self, tasker, noti_type, detail):
        if noti_type != NotificationType.Starting:
            return

        enabled = False
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                raise ValueError("配置必须是 JSON 对象")
            # save_on_error 默认开启，不作为显示调试日志的条件。
            enabled = any(config.get(key) is True for key in (
                "recording", "save_draw", "show_hit_box",
            ))
        except (OSError, ValueError) as exc:
            print(f"[LogConfig] 无法读取调试配置，debug 仅写日志文件：{exc}", flush=True)

        mfaalog.set_debug_ui_enabled(enabled)
