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
    def __init__(self, report, cancelled=lambda: False, *, timeout=300,
                 clock=time.monotonic, sleep=time.sleep):
        self.report = report
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
        until = min(self.clock() + seconds, self.deadline)
        while self.clock() < until:
            self.check()
            self.sleep(min(0.1, until - self.clock()))
        self.check()

    def success(self):
        self.check()
        self.report(f"[启动准备] 完成（{self.clock() - self.started:.1f} 秒）")
