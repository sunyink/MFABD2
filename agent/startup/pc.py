"""PC state machine, independent of native calls for offline verification."""

from .common import Prepared, PreparationError
from .options import PCOptions

TARGET = (1280, 720)


def resize(api, hwnd, budget, target=TARGET):
    until = min(budget.deadline, budget.clock() + 10)
    toggled = False
    original_size = None
    while True:
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
        size = api.client_size(hwnd)
        if original_size is None:
            original_size = size
        if size == target:
            return original_size
        api.resize_client(hwnd, target)
        budget.pause(min(0.5, until - budget.clock()))
        if api.is_game(hwnd) and not api.fullscreen(hwnd) and api.client_size(hwnd) == target:
            return original_size
    raise PreparationError(f"无法将游戏客户区调整为 {target[0]}×{target[1]}，请手动 Alt+Enter 切回窗口模式后重试")


def apply_window_options(api, hwnd, budget, options, source):
    budget.check()
    if not api.is_game(hwnd):
        raise PreparationError("绑定的游戏窗口已失效，请重新连接")
    before = api.client_size(hwnd)
    was_minimized = api.minimized(hwnd)
    was_fullscreen = api.fullscreen(hwnd)
    if not options.minimize and api.pseudo_minimized(hwnd):
        raise PreparationError("游戏窗口仍保持透明，请先关闭其他控制端；若旧控制端已退出，请重启游戏后重新连接")
    # Iconic windows can report 0x0. Measure after restoring instead of
    # treating the iconic size as a real resolution or an optimization hint.
    restored_size = resize(api, hwnd, budget, options.target)
    if was_minimized:
        before = restored_size
    actual = api.client_size(hwnd)
    # Measure and confirm the physical client area before Windows minimizes it.
    if actual != options.target:
        raise PreparationError(f"PC 客户区回读不符：期望 {options.target}，实际 {actual}")
    if options.minimize:
        budget.check()
        if not api.minimized(hwnd) and not api.pseudo_minimized(hwnd):
            api.minimize(hwnd)
        until = min(budget.deadline, budget.clock() + 2)
        while not (api.minimized(hwnd) or api.pseudo_minimized(hwnd)):
            budget.check()
            if not api.is_game(hwnd) or budget.clock() >= until:
                raise PreparationError("2 秒内未确认 PC 窗口最小化状态")
            budget.pause(0.1)
    resized = "已调整" if before != actual else "无需调整"
    restored = "；已退出全屏" if was_fullscreen else "；已还原窗口" if was_minimized and not options.minimize else ""
    mode = "最小化（由框架维持后台渲染）" if options.minimize else "普通窗口"
    budget.report(f"[PC窗口][{source}] 分辨率 {before[0]}×{before[1]} → {actual[0]}×{actual[1]}（{resized}{restored}）；{mode}")


def prepare(api, budget, hwnd=None, options=None):
    options = options or PCOptions()

    # A connected controller must keep its original HWND; never silently rebind.
    if hwnd is not None:
        budget.phase = "校正绑定的 PC 游戏窗口"
        apply_window_options(api, hwnd, budget, options, "sink")
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
            # Keep the window restored for controller connection. Apply the
            # requested minimized mode only after connection, in the task sink.
            connection_options = PCOptions(options.resolution, minimize=False)
            apply_window_options(api, windows[0], budget, connection_options, "pretask")
            if options.minimize:
                budget.report("[PC窗口][pretask] 最小化已留待连接完成后、任务开始前执行")
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
