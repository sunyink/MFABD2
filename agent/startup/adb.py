"""Foreground-only ADB preparation; all commands use the bound controller."""

import re
from enum import Enum

from .common import Prepared, PreparationError

PACKAGE = "com.neowizgames.game.browndust2"
COMPONENT = re.compile(r"(?<![\w.])([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)(?![\w.$])")
ERROR = re.compile(r"permission denial|securityexception|error:|not found|can't find|exception", re.I)
COMMAND_ERROR = re.compile(r"^\s*(?:permission denial\b|(?:java\.lang\.)?securityexception\b|error:|exception\b|/system/bin/sh:|sh:)", re.I)


class Foreground(Enum):
    GAME = 1
    OTHER = 2
    UNKNOWN = 3


def parse_foreground(output, markers):
    if not output:
        return Foreground.UNKNOWN
    lines = output.splitlines()
    if any(COMMAND_ERROR.search(line) for line in lines):
        return Foreground.UNKNOWN
    packages = set()
    for line in lines:
        if any(marker in line for marker in markers):
            match = COMPONENT.search(line)
            if match:
                packages.add(match[1])
    # Multiple displays/activities with conflicting foreground claims are unknown.
    if len(packages) != 1:
        return Foreground.UNKNOWN
    return Foreground.GAME if packages == {PACKAGE} else Foreground.OTHER


def shell(controller, command, budget):
    budget.check()
    timeout = max(1, min(5000, int(budget.remaining * 1000)))
    job = controller.post_shell(command, timeout=timeout)
    # Do not enqueue another probe behind a stuck shell operation.
    until = min(budget.deadline, budget.clock() + timeout / 1000)
    while not job.done:
        budget.check()
        if budget.clock() >= until:
            raise PreparationError("ADB 命令未在限定时间内结束")
        budget.pause(min(0.1, until - budget.clock()))
    budget.check()
    return job.get() if job.succeeded else None


def foreground(controller, budget):
    state = parse_foreground(shell(controller, "dumpsys window windows", budget), ("mCurrentFocus=",))
    if state != Foreground.UNKNOWN:
        return state
    return parse_foreground(
        shell(controller, "dumpsys activity activities", budget),
        ("topResumedActivity=", "mResumedActivity:", "ResumedActivity:"),
    )


def prepare(controller, budget):
    attempted_am = attempted_monkey = changed = False
    first_probe = True
    while True:
        budget.check()
        budget.phase = "等待安卓游戏进入前台"
        state = foreground(controller, budget)
        if state == Foreground.GAME:
            # A game appearing after an unknown probe/manual intervention also
            # needs StartGame's loading recognition branch.
            return Prepared(changed or not first_probe)
        first_probe = False
        if state == Foreground.OTHER:
            if not attempted_am:
                attempted_am = True
                output = shell(controller, f"cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {PACKAGE}", budget)
                matches = COMPONENT.findall(output or "") if not ERROR.search(output or "") else []
                components = {f"{pkg}/{activity}" for pkg, activity in matches if pkg == PACKAGE}
                if len(components) == 1:
                    changed = True
                    budget.report("[启动准备] 尝试使用 am start 拉起游戏")
                    shell(controller, f"am start -n {next(iter(components))}", budget)
            elif not attempted_monkey:
                attempted_monkey = True
                changed = True
                budget.report("[启动准备] 尝试使用 monkey 拉起游戏")
                shell(controller, f"monkey -p {PACKAGE} -c android.intent.category.LAUNCHER 1", budget)
        else:
            budget.phase = "前台状态未知，等待可确认的窗口或 Activity 信息"
        budget.pause(2)
