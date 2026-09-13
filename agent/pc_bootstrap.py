"""PI pretask entry. No AgentServer, resource loading or controller connection."""

import logging
import os
import sys
from pathlib import Path

# The embedded Windows interpreter uses python310._pth and does not add the
# script directory to sys.path. Resolve from this file, not the UI's cwd.
agent_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(agent_dir))

from startup.common import Budget, Cancelled


def main():
    if os.name != "nt":
        return 0
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        # A GUI pretask may have no inherited console handles.
        if stream is None:
            stream = open(os.devnull, "w", encoding="utf-8")
            setattr(sys, name, stream)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parent.parent
    log_dir = root / "debug"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("pc_bootstrap")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_dir / "pc_bootstrap.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)

    def report(message):
        print(message, flush=True)
        logger.info(message)

    from utils.host_watchdog import HostWatchdog

    watchdog = HostWatchdog()
    try:
        from startup.pc import prepare
        from startup.win32 import WindowsAPI

        budget = Budget(report, lambda: watchdog.host_exited(0))
        with WindowsAPI() as api:
            prepare(api, budget)
        budget.success()
        return 0
    except (Cancelled, KeyboardInterrupt):
        report("[启动准备] 已取消；游戏和启动器保持运行")
        return 130
    except Exception as exc:
        report(f"[启动准备] 失败：{exc}")
        return 1
    finally:
        watchdog.close()
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
