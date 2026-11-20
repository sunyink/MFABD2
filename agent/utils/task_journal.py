"""任务起止时刻：框架每报一次任务开始或结束，在内存里记一行。

只取时刻。框架报的成功 / 失败不代表流程正常结束——资源侧的默认兜底让任务几乎总以成功结束，
这里原样留着只为排查。回调里不读写存档、不碰游戏。
"""

from collections import deque
import threading
import time
from uuid import uuid4

from maa.event_sink import NotificationType
from maa.tasker import TaskerEventSink

from .queue_hold import is_queue_hold

# Agent 进程的一次生命周期。进程重启后内存里的记账状态也没了，两边一起算「不连续」。
SESSION = uuid4().hex
# 不算任务的：MFAA 在两个任务之间投递的占位任务（is_queue_hold）；停止任务时框架自己投递的那一条。
SKIP = frozenset({"MaaTaskerPostStop"})

_LOCK = threading.Lock()
_ROWS: deque = deque(maxlen=400)


def record(entry: str, task_id: int, event: str, now: float | None = None) -> None:
    """event：start / succeeded / failed。

    框架只对投递的任务报这类事件，动作里嵌套跑的子任务不报（2026-11 实跑日志：任务号被子任务占掉、事件只有顶层的）。
    新任务开始时上一行还没收到结束，就按此刻补上，免得一次漏报把后面的间隔全算错。
    """
    now = time.time() if now is None else now
    if entry in SKIP or is_queue_hold(entry):
        return
    with _LOCK:
        if event == "start":
            if _ROWS and _ROWS[-1]["end"] is None:
                _ROWS[-1]["end"], _ROWS[-1]["status"] = now, "unknown"
            _ROWS.append({"entry": entry, "task_id": task_id, "start": now, "end": None, "status": None})
            return
        for row in reversed(_ROWS):
            if row["task_id"] == task_id:
                if row["end"] is None:
                    row["end"], row["status"] = now, event
                return


def note(task_id: int, **facts) -> None:
    """给这个任务的那一行补记别的模块留下的事实，随 rows() 一起读得到；任务不在表里就不记。

    现在只有记账位置在用：stock_current——这个任务最近走过的那个记账位置上，库存读数接不接得上
    （不用盘、或盘了为 True，该盘没盘成为 False）。出售与补查据此决定认不认更早的读数（utils/arbitrage_sale_skip.py）。
    """
    with _LOCK:
        for row in reversed(_ROWS):
            if row["task_id"] == task_id:
                row.update(facts)
                return


def rows() -> list:
    """按开始时刻排的任务行副本。"""
    with _LOCK:
        return [dict(row) for row in _ROWS]


def reset() -> None:
    with _LOCK:
        _ROWS.clear()


class TaskJournalSink(TaskerEventSink):
    def on_tasker_task(self, tasker, noti_type, detail):
        try:
            if noti_type == NotificationType.Starting:
                record(detail.entry, detail.task_id, "start")
            elif noti_type == NotificationType.Succeeded:
                record(detail.entry, detail.task_id, "succeeded")
            elif noti_type == NotificationType.Failed:
                record(detail.entry, detail.task_id, "failed")
        except Exception as exc:  # 监听里出错不能影响任务
            print(f"[TaskJournal] 记录任务起止失败：{exc}", flush=True)
