"""Per-task startup gate. A failure fails the current task, not the whole session."""

from collections import OrderedDict

from . import adb
from .common import Budget, Cancelled, Prepared, PreparationError
from .options import PCOptions

# 哪些控制器保留「会话级失败记忆」。
# PC 已改为每任务重试，窗口准备使用独立的短预算。
# ADB 仍是 300 秒预算——去掉记忆会让模拟器掉线时 12 个任务变成最多 60 分钟空等，
# 比「一次失败全灭」更折磨人。要一并放开，得先给 ADB 加失败冷却或收紧预算。
PERSIST_FAILURE_KINDS = ("adb",)


class StartupGuard:
    def __init__(self, report, error, *, warn=None, budget_factory=Budget,
                 adb_prepare=adb.prepare, pc_prepare=None):
        self.report = report
        self.error = error
        self.warn = warn or report
        self.budget_factory = budget_factory
        self.adb_prepare = adb_prepare
        self.pc_prepare = pc_prepare or self._pc_prepare
        self.tasks = OrderedDict()
        self.failures = {}

    @staticmethod
    def _pc_prepare(controller, info, budget, options):
        # 签名保持四参：它是 pc_prepare 的注入契约，多处测试按四参断言。controller
        # 本身自 post_inactive 救援删除后就不再需要（见下），显式 del 表明这是有意的。
        del controller
        from .pc import prepare
        from .win32 import WindowsAPI

        hwnd = info.get("hwnd")
        if isinstance(hwnd, str):
            hwnd = int(hwnd, 0)
        if not isinstance(hwnd, int) or not hwnd:
            raise PreparationError("Win32 控制器未提供有效的窗口句柄")
        # 这里曾用 post_inactive() 试图让框架「恢复它保存的扩展样式」，那是误解：
        # post_inactive 的语义是「恢复窗口位置（取消置顶）并解除输入阻断」，完全不碰
        # layered/alpha。于是救援空转、随后必然二次报错。伪最小化是框架自己的后台截图
        # 机制，既不需要也无法主动退出——详见 pc.align_window 里的说明。别再加回来。
        with WindowsAPI() as api:
            return prepare(api, budget, hwnd=hwnd, options=options)

    @staticmethod
    def stop(tasker):
        # Waiting here deadlocks the task whose callback is currently executing.
        tasker.post_stop()

    def _remember(self, key, result):
        if key is not None:
            self.tasks[key] = result
            if len(self.tasks) > 256:
                self.tasks.popitem(last=False)
        return result

    def ensure(self, context, task_id=None):
        tasker = context.tasker
        key = None
        try:
            if task_id is None:
                # Nested run_task events have a new task id; their cloned Context
                # still identifies the top-level task that owns this preparation.
                task_id = context.get_task_job().job_id
            # 先落一个只带 task_id 的键，这样连「查控制器身份」本身失败也会被记住，
            # 不会在同一个任务的后续每个节点上反复重试、反复 post_stop。
            key = (None, None, task_id)
            controller = tasker.controller
            info = controller.info
            kind = info.get("type")
            if kind not in ("adb", "win32"):
                return Prepared()
            uuid = controller.uuid
            if not uuid:
                raise PreparationError("无法取得控制器的稳定身份")
            # key 必须先补全再判 latch：否则重放的失败会带着不完整的 key 落进
            # except，被当成「身份查询失败」而报出错误的收场文案。
            key = (kind, uuid, task_id)
            if kind in PERSIST_FAILURE_KINDS and (kind, uuid) in self.failures:
                raise PreparationError(self.failures[(kind, uuid)])
            # 同一任务内命中即短路；值为 None 表示这个任务已经判过失败，于是
            # StartGame 的三级 on_error 阶梯不会重跑准备，也不会重复刷错误日志。
            if key in self.tasks:
                return self.tasks[key]
            if kind == "adb":
                budget = self.budget_factory(self.report, lambda: tasker.stopping)
                result = self.adb_prepare(controller, budget)
            else:
                from .pc import PC_TASK_TIMEOUT

                # PC 只等待窗口校正与最小化，不沿用启动器更新的 300 秒预算。
                budget = self.budget_factory(self.report, lambda: tasker.stopping,
                                             timeout=PC_TASK_TIMEOUT, warn=self.warn)
                options = PCOptions.from_context(context)
                result = self.pc_prepare(controller, info, budget, options)
            budget.success()
            return self._remember(key, result)
        except Cancelled as exc:
            self.report(f"[启动准备] {exc}")
            self.stop(tasker)
            return self._remember(key, None)
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            if key is not None and key[0] in PERSIST_FAILURE_KINDS:
                self.failures[(key[0], key[1])] = reason
                self.error(f"[启动准备] {reason}；本会话该控制器的后续任务将停止执行，请修复后重新启动软件")
            else:
                self.error(f"[启动准备] {reason}；本任务已停止，下个任务会重新检查（不需要重启软件）")
            self.stop(tasker)
            return self._remember(key, None)
