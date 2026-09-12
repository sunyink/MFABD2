"""PC state machine, independent of native calls for offline verification."""

from .common import Prepared, PreparationError

TARGET = (1280, 720)


def resize(api, hwnd, budget):
    until = min(budget.deadline, budget.clock() + 10)
    toggled = False
    for _ in range(5):
        budget.check()
        if budget.clock() >= until:
            break
        if not api.is_game(hwnd):
            raise PreparationError("绑定的游戏窗口已失效，请重新连接")
        if api.restore(hwnd) is False:
            budget.pause(min(0.2, until - budget.clock()))
            continue
        if api.fullscreen(hwnd):
            if not toggled:
                api.exit_fullscreen(hwnd)
                toggled = True
            budget.pause(min(1, until - budget.clock()))
            continue
        if api.client_size(hwnd) == TARGET:
            return
        api.resize_client(hwnd, TARGET)
        budget.pause(min(0.5, until - budget.clock()))
        if api.is_game(hwnd) and not api.fullscreen(hwnd) and api.client_size(hwnd) == TARGET:
            return
    raise PreparationError("无法将游戏客户区调整为 1280×720，请手动 Alt+Enter 切回窗口模式后重试")


def prepare(api, budget, hwnd=None):
    # A connected controller must keep its original HWND; never silently rebind.
    if hwnd is not None:
        budget.phase = "校正绑定的 PC 游戏窗口"
        resize(api, hwnd, budget)
        return Prepared()
    launched = False
    last_dialogs = None
    while True:
        budget.check()
        windows, running, dialogs = api.scan()
        if len(windows) > 1:
            raise PreparationError("检测到多个棕色尘埃2游戏窗口，请保留需要连接的一个")
        if windows:
            budget.phase = "校正 PC 游戏窗口"
            resize(api, windows[0], budget)
            return Prepared(launched)
        if dialogs != last_dialogs:
            if dialogs:
                budget.report(f"[启动准备] 启动器窗口：{', '.join(dialogs)}；继续观察，不自动点击弹框")
            last_dialogs = dialogs
        if not running and not launched:
            api.launch()
            launched = True
            budget.report("[启动准备] 已调用官方启动器")
        budget.phase = "等待启动器更新并交接游戏主窗口"
        budget.pause(2)
