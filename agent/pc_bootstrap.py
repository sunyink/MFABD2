"""PI pretask entry. No AgentServer, resource loading or controller connection.

职责只有两件：游戏没在跑就拉起官方启动器，然后等到主窗口出现并确认它存在。
窗口分辨率与最小化一律交给 agent 在任务首节点（startup.sink）处理——那时控制器
已经连上，改窗口既不会干扰连接，也不必猜上层软件的时序。

本进程永远以 0 退出。PI 协议对 pretask 的退出码没有任何规定（见
deps/tools/interface.schema.json 的 pretaskConfig），返回非 0 是否中止整个任务
队列属于上层软件的实现自由度，赌不得。拉不起游戏时让控制器连接自然失败即可——
那是 pretask 出现之前的原状，用户会看到「连接失败」。
"""

import os
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent


def _boot_log(directory=None, note=""):
    """把 cwd / argv / 解释器路径落盘。必须在任何项目 import 之前调用。

    线上排查过一次「4 次 pretask 只有 2 次写出日志」，当时无法区分两种原因：
    进程根本没起来，还是起来了但 import 阶段就死了。有这一行就能一刀切开，
    并且直接读出上层软件给的工作目录到底是哪个。

    自身绝不抛异常、绝不影响启动：debug 目录不可写（例如装在 Program Files）
    时退到 %TEMP%，全部候选都失败就静默放弃。
    """
    line = (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} [boot] pid={os.getpid()} "
        f"cwd={os.getcwd()!r} exe={sys.executable!r} argv={sys.argv!r} file={__file__!r}"
        + (f" note={note}" if note else "")
        + "\n"
    )
    for target in (directory, AGENT_DIR.parent / "debug", Path(os.environ.get("TEMP", "."))):
        if target is None:
            continue
        try:
            path = Path(target)
            path.mkdir(parents=True, exist_ok=True)
            with open(path / "pc_bootstrap.log", "a", encoding="utf-8") as handle:
                handle.write(line)
            return path
        except Exception:
            continue
    return None


# The embedded Windows interpreter uses python310._pth and does not add the
# script directory to sys.path. Resolve from this file, not the UI's cwd.
sys.path.insert(0, str(AGENT_DIR))


def _run(report):
    """真正的准备流程。所有项目导入都在这里，好让 import 失败也能被写进日志。"""
    from startup.common import Budget, Cancelled
    from startup.pc import prepare
    from startup.win32 import WindowsAPI
    from utils.host_watchdog import HostWatchdog

    watchdog = HostWatchdog()
    try:
        budget = Budget(report, lambda: watchdog.host_exited(0))
        # argv 只进 [boot] 日志、不解析：pretask 不再消费任何选项，因此参数形状
        # 将来怎么变都不该让启动失败。
        with WindowsAPI() as api:
            prepare(api, budget)
        budget.success()
    except (Cancelled, KeyboardInterrupt):
        report("[启动准备] 已取消；游戏和启动器保持运行")
    finally:
        watchdog.close()


def main():
    if os.name != "nt":
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        # A GUI pretask may have no inherited console handles.
        if stream is None:
            stream = open(os.devnull, "w", encoding="utf-8")
            setattr(sys, name, stream)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = AGENT_DIR.parent / "debug"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pc_bootstrap")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_dir / "pc_bootstrap.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)

    def report(message):
        print(message, flush=True)
        logger.info(message)

    try:
        _run(report)
    except Exception as exc:
        report(f"[启动准备] 失败：{exc}；不阻断任务队列，若游戏未启动会在连接阶段报错")
    finally:
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    _boot_log()
    try:
        main()
    except BaseException as exc:
        # 连 logger 都没建起来（例如 debug 目录不可写）才会走到这里。
        _boot_log(note=f"unhandled {exc!r}")
    # 永远以 0 退出，理由见模块 docstring。
    raise SystemExit(0)
