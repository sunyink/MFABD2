"""Per-task PC startup gate; ADB preparation is opt-in for research only."""

from collections import OrderedDict
from functools import partial

from .common import Budget, Cancelled, Prepared, PreparationError
from .options import PCOptions

# 哪些控制器保留「会话级失败记忆」。
# PC 已改为每任务重试，窗口准备使用独立的短预算。
# ADB 已默认停用；显式注入旧算法的研究测试仍使用以下失败记忆。
# 旧算法是 300 秒预算——去掉记忆会让模拟器掉线时 12 个任务变成最多 60 分钟空等，
# 比「一次失败全灭」更折磨人。要一并放开，得先给 ADB 加失败冷却或收紧预算。
PERSIST_FAILURE_KINDS = ("adb",)


class StartupGuard:
    def __init__(self, report, error, *, warn=None, budget_factory=Budget,
                 adb_prepare=None, pc_prepare=None):
        self.report = report
        self.error = error
        self.warn = warn or report
        self.budget_factory = budget_factory
        self.adb_prepare = adb_prepare
        self.pc_prepare = pc_prepare or partial(self._pc_prepare, watch=self._keep_minimized)
        self.tasks = OrderedDict()
        self.failures = {}
        # 当前「最小化保持」线程的停止信号；新任务或任务结束时置位。
        self.minimize_watch = None

    @staticmethod
    def _pc_prepare(controller, info, budget, options, watch=None):
        # controller 保留在注入契约中；窗口准备本身只使用原生窗口接口。
        del controller
        from .pc import prepare
        from .win32 import WindowsAPI

        hwnd = info.get("hwnd")
        if isinstance(hwnd, str):
            hwnd = int(hwnd, 0)
        if not isinstance(hwnd, int) or not hwnd:
            raise PreparationError("Win32 控制器未提供有效的窗口句柄")
        # MaaFw 5.12.3 的 post_inactive() 确实会撤销当前截图控制器的伪最小化。
        # 但多个控制器可能把彼此制造的透明状态保存成原始状态，inactive 也会恢复成
        # 透明。应避免重复截图控制器，不能在每个任务中靠 inactive 掩盖该生命周期问题。
        with WindowsAPI() as api:
            result = prepare(api, budget, hwnd=hwnd, options=options)
            # 只在 sink 真的把窗口压下去之后盯；它判定「本次不最小化」时窗口本来就该亮着。
            if watch and options.minimize and (api.minimized(hwnd) or api.pseudo_minimized(hwnd)):
                watch(hwnd)
        return result

    def _keep_minimized(self, hwnd):
        """后台线程跑 pc.keep_minimized，不阻塞 pipeline；任务结束（task_finished）即停。"""
        import threading
        import time

        from .pc import keep_minimized
        from .win32 import WindowsAPI

        self.task_finished()
        stop = self.minimize_watch = threading.Event()

        def run():
            try:
                with WindowsAPI() as api:
                    keep_minimized(api, hwnd, clock=time.monotonic, pause=stop.wait,
                                   stopped=stop.is_set, report=self.report)
            except Exception as exc:
                self.warn(f"[PC窗口] 最小化保持检查出错（{str(exc) or type(exc).__name__}），不影响任务")

        threading.Thread(target=run, name="PCKeepMinimized", daemon=True).start()

    def task_finished(self):
        if self.minimize_watch is not None:
            self.minimize_watch.set()

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
            # ADB 公共准备暂时停用；生产 sink 不注入 adb_prepare。
            # 在身份、失败记忆和预算检查前旁路，旧算法只留给独立研究验证。
            if kind == "adb" and self.adb_prepare is None:
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
            # 只有 PreparationError（超时/取不到身份/命令未结束等环境类判据）才配
            # 上升为会话级失败。代码级异常是 bug，按任务级失败处理——否则一次偶发
            # 就把整轮队列全灭，而重试一次的代价只有 0.2 秒。
            if not isinstance(exc, PreparationError):
                self.error(f"[启动准备] {type(exc).__name__}：{reason}；本任务已停止，"
                           "下个任务会重新检查（不需要重启软件）")
                self.stop(tasker)
                return self._remember(key, None)
            if key is not None and key[0] in PERSIST_FAILURE_KINDS:
                self.failures[(key[0], key[1])] = reason
                self.error(f"[启动准备] {reason}；本会话该控制器的后续任务将停止执行，请修复后重新启动软件")
            else:
                self.error(f"[启动准备] {reason}；本任务已停止，下个任务会重新检查（不需要重启软件）")
            self.stop(tasker)
            return self._remember(key, None)
