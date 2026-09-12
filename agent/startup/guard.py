"""Per-task gate with failures retained for the lifetime of the agent session."""

from collections import OrderedDict

from . import adb
from .common import Budget, Cancelled, Prepared, PreparationError


class StartupGuard:
    def __init__(self, report, error, *, budget_factory=Budget, adb_prepare=adb.prepare, pc_prepare=None):
        self.report = report
        self.error = error
        self.budget_factory = budget_factory
        self.adb_prepare = adb_prepare
        self.pc_prepare = pc_prepare or self._pc_prepare
        self.tasks = OrderedDict()
        self.failures = {}
        self.identity_failure = None

    @staticmethod
    def _pc_prepare(controller, info, budget):
        from .pc import prepare
        from .win32 import WindowsAPI

        hwnd = info.get("hwnd")
        if isinstance(hwnd, str):
            hwnd = int(hwnd, 0)
        if not isinstance(hwnd, int) or not hwnd:
            raise PreparationError("Win32 控制器未提供有效的窗口句柄")
        with WindowsAPI() as api:
            return prepare(api, budget, hwnd=hwnd)

    @staticmethod
    def stop(tasker):
        # Waiting here deadlocks the task whose callback is currently executing.
        tasker.post_stop()

    def ensure(self, context, task_id=None):
        tasker = context.tasker
        key = None
        try:
            if self.identity_failure:
                raise PreparationError(self.identity_failure)
            controller = tasker.controller
            info = controller.info
            kind = info.get("type")
            if kind not in ("adb", "win32"):
                return Prepared()
            uuid = controller.uuid
            if not uuid:
                raise PreparationError("无法取得控制器的稳定身份")
            key = (kind, uuid)
            if key in self.failures:
                raise PreparationError(self.failures[key])
            if task_id is None:
                task_id = context.get_task_job().job_id
            task_key = (key, task_id)
            if task_key in self.tasks:
                return self.tasks[task_key]
            budget = self.budget_factory(self.report, lambda: tasker.stopping)
            if kind == "adb":
                result = self.adb_prepare(controller, budget)
            else:
                result = self.pc_prepare(controller, info, budget)
            budget.success()
            self.tasks[task_key] = result
            if len(self.tasks) > 256:
                self.tasks.popitem(last=False)
            return result
        except Cancelled as exc:
            self.report(f"[启动准备] {exc}")
            self.stop(tasker)
            return None
        except Exception as exc:
            reason = str(exc)
            if key is None:
                self.identity_failure = reason
            else:
                self.failures[key] = reason
            self.error(f"[启动准备] {reason}；本会话后续任务将停止执行，请修复后重新启动软件")
            self.stop(tasker)
            return None
