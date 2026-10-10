"""Shared deadline and progress reporting for both startup entry points."""

import time
from dataclasses import dataclass


class PreparationError(RuntimeError):
    pass


class Cancelled(PreparationError):
    pass


@dataclass(frozen=True)
class Prepared:
    changed: bool = False


class Budget:
    # timeout 是浮点秒数：ADB 用默认 300，PC 的 sink 用 5，验证脚本用 0.5。
    def __init__(self, report, cancelled=lambda: False, *, timeout: float = 300,
                 clock=time.monotonic, sleep=time.sleep, warn=None):
        self.report = report
        # 降级提示必须让用户看见。mfaalog 有独立的 warn 前缀，走 info 会被埋在
        # 一堆常规日志里。没给就退回 report，调用点因此不必都传。
        self.warn = warn or report
        self.cancelled = cancelled
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.deadline = self.started + timeout
        self.next_progress = self.started + 30
        self.phase = "检查游戏状态"
        report(f"[启动准备] 开始，最多等待 {timeout} 秒")

    @property
    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def check(self):
        if self.cancelled():
            raise Cancelled("启动准备已取消")
        now = self.clock()
        if now >= self.deadline:
            raise PreparationError(f"启动准备超时（{self.deadline - self.started:g} 秒）：{self.phase}")
        if now >= self.next_progress:
            self.report(f"[启动准备] 已等待 {int(now - self.started)} 秒：{self.phase}")
            self.next_progress += (int((now - self.next_progress) // 30) + 1) * 30

    def pause(self, seconds):
        # remaining 必须在 check() 之前取一次并复用：check() 本身要读时钟、判停止，
        # 若在 sleep 参数里再读一次时钟，余量可能在两次读取之间耗尽变成负值，
        # time.sleep(负) 会抛 "sleep length must be non-negative"，经 guard 记录成
        # 会话级失败后整轮任务全灭。多睡一点无害，睡负数直接崩。
        until = min(self.clock() + seconds, self.deadline)
        while True:
            remaining = until - self.clock()
            if remaining <= 0:
                break
            self.check()
            self.sleep(min(0.1, remaining))
        self.check()

    def success(self):
        # Preparation has already confirmed readiness. Reporting must not turn
        # that result into a timeout, but cancellation still stops the task.
        if self.cancelled():
            raise Cancelled("启动准备已取消")
        self.report(f"[启动准备] 完成（{self.clock() - self.started:.1f} 秒）")
