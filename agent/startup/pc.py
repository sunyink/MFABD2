"""PC state machine, independent of native calls for offline verification."""

from .common import Cancelled, Prepared, PreparationError
from .options import PCOptions

TARGET = (1280, 720)
# 原生窗口调用是异步投递。冷启动日志中尺寸到第九秒才改变；旧实现五秒内
# 重发十次后就最小化，残留的窗口操作随后又把游戏带回前台。现在每种请求只发
# 一次，回读稳定后才能进入下一阶段。超时可以放行可用画面，但不能放行最小化。
RESIZE_WINDOW = 15.0
GEOMETRY_STABLE_WINDOW = 0.5
# 最小化同样只投递一次，再等待系统/框架确认状态。
MINIMIZE_WINDOW = 6.0
# 客户区长宽比相对目标的容许偏差。短边 720 上 1px 取整误差是 0.14%，这里留足余量。
ASPECT_TOLERANCE = 0.02
# 短边低于这个值时，框架的短边缩放会变成放大，识别精度会掉。此时即使长宽比对得上
# 也要单独提示，不能笼统说「不影响准确度」（实机遇到过 1006×565）。
MIN_SHORT_SIDE = TARGET[1]
# sink 的总预算。必须大于 RESIZE_WINDOW + MINIMIZE_WINDOW，否则总超时会先于长宽比
# 分级和最小化等待触发，那两套兜底就永远走不到。
PC_TASK_TIMEOUT = 22.0
# pretask 亲手拉起游戏后，留给界面线程的稳定时间。游戏本来就在跑时不花这个钱。
SETTLE_AFTER_LAUNCH = 3.0


class ResolutionUnavailable(PreparationError):
    """目标客户区不可达。窗口失效、取消与总超时仍走原有致命路径。"""


def aspect_deviation(size, target=TARGET):
    """size 与 target 长宽比的相对偏差；无法测量时返回 None。"""
    try:
        width, height = size
        return abs((width / height) / (target[0] / target[1]) - 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _fmt(size):
    try:
        return f"{size[0]}×{size[1]}"
    except (TypeError, IndexError, KeyError):
        return repr(size)


def resize(api, hwnd, budget, target=TARGET):
    until = min(budget.deadline, budget.clock() + RESIZE_WINDOW)
    restore_requested = exit_requested = resize_requested = False
    stable_since = None
    original_size = None
    while True:
        budget.check()
        if budget.clock() >= until:
            break
        if not api.is_game(hwnd):
            raise PreparationError("绑定的游戏窗口已失效，请重新连接")
        if api.minimized(hwnd) or api.maximized(hwnd):
            if not restore_requested:
                api.restore(hwnd)
                restore_requested = True
            stable_since = None
        elif api.fullscreen(hwnd):
            if not exit_requested:
                api.exit_fullscreen(hwnd)
                exit_requested = True
            stable_since = None
        else:
            size = api.client_size(hwnd)
            if original_size is None:
                original_size = size
            if size == target:
                if stable_since is None:
                    stable_since = budget.clock()
                if budget.clock() - stable_since >= GEOMETRY_STABLE_WINDOW:
                    return original_size
            else:
                stable_since = None
                if not resize_requested:
                    api.resize_client(hwnd, target)
                    resize_requested = True
                    budget.report(f"[PC窗口][sink] 已请求调整 {_fmt(size)} → {_fmt(target)}；等待尺寸稳定后再处理最小化")
        budget.pause(min(0.1, until - budget.clock()))
    # 只说明「没调成」。是否致命由 align_window 按长宽比统一裁决。
    raise ResolutionUnavailable(f"无法将游戏客户区调整为 {_fmt(target)}")


def align_window(api, hwnd, budget, options):
    """任务首节点（sink）路径：校正已连接窗口的分辨率，并按需最小化。"""
    budget.check()
    if not api.is_game(hwnd):
        raise PreparationError("绑定的游戏窗口已失效，请重新连接")
    before = api.client_size(hwnd)
    was_minimized = api.minimized(hwnd)
    was_fullscreen = api.fullscreen(hwnd)
    restored_size = None
    geometry_ready = False
    try:
        restored_size = resize(api, hwnd, budget, options.target)
        geometry_ready = True
    except ResolutionUnavailable:
        # 先量完再裁决：长宽比对得上时框架的短边缩放能兜住，不该为此杀任务。
        pass
    actual = api.client_size(hwnd)
    geometry_ready = geometry_ready and actual == options.target
    if was_minimized:
        # Iconic windows can report 0x0. Measure after restoring instead of
        # treating the iconic size as a real resolution or an optimization hint.
        before = restored_size if restored_size is not None else actual
    note = ""
    if actual != options.target:
        deviation = aspect_deviation(actual, options.target)
        if deviation is None or deviation > ASPECT_TOLERANCE:
            raise PreparationError(
                f"游戏客户区为 {_fmt(actual)}，无法调整为 {_fmt(options.target)}，"
                + ("且长宽比无法测量" if deviation is None else f"且长宽比偏离目标达 {deviation:.1%}")
                + "，识别坐标会整体错位。请手动 Alt+Enter 切回窗口模式，"
                "或在游戏内把分辨率改成 16:9 后重试")
        short_side = min(actual)
        if short_side < MIN_SHORT_SIDE:
            note = (
                f"；未能改成 {_fmt(options.target)}，长宽比虽与目标一致（偏差 {deviation:.1%}），"
                f"但短边只有 {short_side} 像素、不足 {MIN_SHORT_SIDE}，框架需要放大画面才能识别，"
                "OCR 与模板匹配的精度可能下降；本任务继续执行，下个任务会重新尝试调整"
            )
        else:
            note = (
                f"；提示：未能改成 {_fmt(options.target)}，但长宽比与目标一致（偏差 {deviation:.1%}），"
                "框架会把短边缩放到 720 再识别；本任务继续执行，下个任务会重新尝试调整"
            )
    mode = "普通窗口"
    if not options.minimize and api.pseudo_minimized(hwnd):
        # 伪最小化是 MaaFramework 自己的后台截图机制：FramePool / PrintWindow 在窗口被
        # 最小化时把它设为透明并开启点击穿透，以不激活的方式恢复，从而继续截图；框架的
        # monitor 线程会持续 apply/revert。它对截图与输入都完全正常，既不是故障，也没有
        # 任何公开 API 能令其回退（post_inactive 只管取消置顶与解除输入阻断）。
        # 所以这里只报告状态，绝不报错——把它当故障曾让整个任务队列全灭。
        mode = (
            "窗口当前处于框架的后台截图模式（透明并点击穿透，这是 MaaFramework 自身机制，"
            "不影响识别）；点任务栏中的游戏即可恢复查看"
        )
    if options.minimize and not geometry_ready:
        mode = "窗口尺寸尚未确认稳定，本次不再最小化；下个任务重新检查"
    elif options.minimize:
        try:
            budget.check()
            if not api.minimized(hwnd) and not api.pseudo_minimized(hwnd):
                budget.report(f"[PC窗口][sink] 尺寸 {_fmt(actual)} 已稳定；请求最小化")
                api.minimize(hwnd)
            until = min(budget.deadline, budget.clock() + MINIMIZE_WINDOW)
            while not (api.minimized(hwnd) or api.pseudo_minimized(hwnd)):
                budget.check()
                if not api.is_game(hwnd):
                    raise PreparationError("绑定的游戏窗口已失效，请重新连接")
                if budget.clock() >= until:
                    raise PreparationError(
                        f"请求已发出，但 {MINIMIZE_WINDOW:g} 秒内未确认最小化状态"
                        "（窗口可能稍后自行最小化）")
                budget.pause(0.1)
            mode = "最小化状态已确认"
        except Cancelled:
            raise
        except Exception as exc:
            # Only the optional minimize step is best-effort. Cancellation,
            # the shared deadline and a lost game window remain fatal.
            budget.check()
            if not api.is_game(hwnd):
                raise PreparationError("绑定的游戏窗口已失效，请重新连接") from exc
            reason = str(exc) or type(exc).__name__
            mode = f"提示：最小化未确认（{reason}）；保持当前窗口状态并继续任务，下个任务重新检查"
    # 全屏与还原都读当前状态：降级路径下可能「没退出全屏但客户区恰好达标」，
    # 只看初始状态会让这句话变成假话。
    still_fullscreen = api.fullscreen(hwnd)
    if was_fullscreen and not still_fullscreen:
        extra = "；已退出全屏"
    elif still_fullscreen:
        extra = "；仍为全屏"
    elif was_minimized and not options.minimize:
        extra = "；已还原窗口"
    else:
        extra = ""
    resized = "未达目标" if note else "已调整" if before != actual else "无需调整"
    line = f"[PC窗口][sink] 分辨率 {_fmt(before)} → {_fmt(actual)}（{resized}{extra}）；{mode}{note}"
    # 降级整行走 warn，否则这条「为什么继续跑」会被埋在一堆 info 里。
    (budget.warn if note or not geometry_ready else budget.report)(line)


def settle_after_launch(api, hwnd, budget):
    """给亲手拉起的游戏一段界面线程就绪时间，期间持续确认窗口没有消失。

    返回 True 表示窗口稳定存活；False 表示它在就绪前消失了（启动失败会闪退），
    此时该退回等待循环，而不是把一个已失效的句柄交给 sink。

    为什么是固定等待而不是探测：试过用 SendMessageTimeout(WM_NULL) 探窗口的消息
    循环还转不转，但实机数据否掉了它——投递的最小化请求 1.7~2.2 秒后确实被处理了，
    不是一直排队到超时，说明游戏加载期消息循环是在转的、只是处理得慢。那么探针会
    立刻返回「空闲」，等于没探。
    """
    budget.phase = "等待新启动的游戏窗口就绪"
    until = min(budget.deadline, budget.clock() + SETTLE_AFTER_LAUNCH)
    while budget.clock() < until:
        budget.pause(min(0.5, until - budget.clock()))
        if not api.is_game(hwnd):
            return False
    return True


def launch_and_confirm(api, budget):
    """pretask 路径：游戏没跑就拉起官方启动器，等到主窗口出现并确认它存在。

    刻意不接收 options——窗口尺寸与最小化一律由任务首节点的 sink 负责。这里在签名
    层面就拿不到那两个配置，因此不会再退化成「顺手 resize 一下」；也不调 restore()，
    连接前不抢用户焦点。被手动最小化的窗口原样交给软件连接，框架首次截图时自会处理。

    唯一的例外是「本轮亲手拉起过游戏」：那种窗口刚出现时界面线程正忙于加载，交出去
    会让 sink 的 resize 与最小化全部落空（实机实测过）。这段等待放在这里而不是 sink
    里，是因为 pretask 跑在独立进程中，等它多久都不会阻塞 pipeline。
    """
    launched = False
    last_dialogs = None
    while True:
        budget.check()
        windows, running, dialogs = api.scan()
        if windows:
            # 多窗口交给软件自己的窗口选择，这里只报数不拦——pretask 没资格替用户决定。
            extra = f"（检测到 {len(windows)} 个，由软件选择连接目标）" if len(windows) > 1 else ""
            budget.report(f"[启动准备] 已确认游戏主窗口{extra}；窗口尺寸与最小化在任务开始时处理")
            # 游戏本来就在跑时窗口早已稳定，不花这段等待。
            if not launched:
                return Prepared(launched)
            if settle_after_launch(api, windows[0], budget):
                budget.report(
                    f"[启动准备] 新窗口已稳定 {SETTLE_AFTER_LAUNCH:g} 秒。注意：游戏是本轮新启动的，"
                    "窗口编号已经变了；若软件随后报连接失败，点一次「刷新连接目标」让它重新识别窗口")
                return Prepared(launched)
            budget.report("[启动准备] 新窗口在就绪前消失，继续等待启动器交接")
            continue
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


def prepare(api, budget, hwnd=None, options=None):
    # A connected controller must keep its original HWND; never silently rebind.
    if hwnd is not None:
        budget.phase = "校正绑定的 PC 游戏窗口"
        align_window(api, hwnd, budget, options or PCOptions())
        return Prepared()
    budget.phase = "确认 PC 游戏主窗口"
    return launch_and_confirm(api, budget)
