"""MFAA 队列占位任务的识别：新旧客户端的名字都要认，别的名字一个都不能误认。纯离线，不加载原生库。

python tools/test_queue_hold.py
"""

import sys
import types
import unittest
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

# task_journal 只用到这两个名字；换成桩，测试不碰 maa 包（导入即加载原生库）。
_event_sink = types.ModuleType("maa.event_sink")
setattr(_event_sink, "NotificationType", Enum("NotificationType", "Starting Succeeded Failed"))
_tasker = types.ModuleType("maa.tasker")
setattr(_tasker, "TaskerEventSink", object)
sys.modules.setdefault("maa", types.ModuleType("maa"))
sys.modules.setdefault("maa.event_sink", _event_sink)
sys.modules.setdefault("maa.tasker", _tasker)

from utils import task_journal  # noqa: E402
from utils.queue_hold import is_queue_hold  # noqa: E402

# 上游 v2.16.6-beta.1 QueueHold.cs：NodeName = "MFAA_QueueHold_" + Guid.NewGuid().ToString("N")
NEW_NAME = "MFAA_QueueHold_0123456789abcdef0123456789abcdef"


class QueueHoldNameTests(unittest.TestCase):
    def test_both_client_generations(self):
        self.assertTrue(is_queue_hold("MFAA_QueueHold"))
        self.assertTrue(is_queue_hold(NEW_NAME))

    def test_other_names(self):
        for name in ("", None, 0, "MFAA_QueueHoldAction", "MFAA_QueueHoldAction_0123", "mfaa_queuehold",
                     "MFAA_QueueHol", "X_MFAA_QueueHold_1", "MaaTaskerPostStop", "StartGame_Entry"):
            with self.subTest(name=name):
                self.assertFalse(is_queue_hold(name))


class TaskJournalSkipTests(unittest.TestCase):
    def setUp(self):
        task_journal.reset()

    def test_holds_between_tasks_leave_no_rows(self):
        task_journal.record("Daily_Entry", 1, "start", now=10.0)
        task_journal.record("Daily_Entry", 1, "succeeded", now=20.0)
        for task_id, name in ((2, "MFAA_QueueHold"), (3, NEW_NAME)):
            task_journal.record(name, task_id, "start", now=21.0)
            task_journal.record(name, task_id, "succeeded", now=22.0)
        task_journal.record("MaaTaskerPostStop", 4, "start", now=23.0)
        task_journal.record("Shop_Entry", 5, "start", now=30.0)
        rows = task_journal.rows()
        self.assertEqual([row["entry"] for row in rows], ["Daily_Entry", "Shop_Entry"])
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertIsNone(rows[1]["end"])


if __name__ == "__main__":
    unittest.main()
