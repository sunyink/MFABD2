"""开发侧虚拟环境：Agent 启动时只检查，装依赖交给独立的同步进程。

Agent 启动处在客户端的连接计时里（VS Code 插件按设置项 maa.agentTimeout，MFAA 不限时），
时长不归我们管：在里面跑 pip，超时就被客户端直接结束，pip 可能停在「卸了旧版、没装新版」。
所以拆成两半：
  · ensure_venv()：毫秒级、不联网。比对 requirements 指纹与 maafw 版本，目标版本取客户端
    按 PI V2 注入的 PI_CLIENT_MAAFW_VERSION（客户端已加载原生库的 MaaVersion()）。
    不一致就在新窗口拉起同步，等它完成后接着启动。客户端先等不及结束了 Agent 也无妨：
    同步在独立进程里不受牵连，窗口关闭后重新启动即可。
  · sync()：本文件作为脚本运行，建 venv、装依赖，全程持锁。首次搭环境也用它：
        py -3.10 agent/utils/venv_ops.py
发布包没有 requirements.txt，不走这里；用户侧从不当场下载。
"""

import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from . import mfaalog  # 日志工具
# 作为脚本运行时不导入 utils 包：首次搭环境时它依赖的第三方库还没装。

# =========================================================
# [配置区] 环境与依赖设定
# =========================================================
# maafw 的版本不手写，跟随客户端（见文件头）。
CLIENT_MAAFW_ENV = "PI_CLIENT_MAAFW_VERSION"
# 客户端没报版本、环境里也还没装 maafw 时，按此范围安装。
FALLBACK_MAAFW_SPEC = ">=5.11,<6.1"
VENV_NAME = ".venv"
PREFERRED_PYTHON_VERSION = "3.10"
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
# =========================================================
# 以下状态文件都在 .venv 里，删掉 .venv 一并清空。
MARKER_NAME = ".deps_marker"  # 上次装成功的 requirements 指纹
LOCK_NAME = ".sync.lock"  # 同步进程持有；进程没了锁由系统释放，不会残留
FAILED_NAME = ".sync_failed"  # 上次没同步成的目标状态，同一目标不再反复拦启动
REFUSED_NAME = ".sync_refused"  # 同步进程拒绝开工的原因，交给拉起它的 Agent 转述
SYNC_WINDOW_TITLE = "MFABD2 开发环境同步"


def get_venv_path(project_root: Path) -> Path:
    return project_root / VENV_NAME

def get_venv_executable(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"
    else:
        return venv_path / "bin" / "python"

def is_running_in_venv() -> bool:
    return sys.prefix != sys.base_prefix

def is_running_in(venv_path: Path) -> bool:
    try:
        return os.path.samefile(sys.prefix, venv_path)
    except OSError:
        return False

def requirements_fingerprint(project_root: Path) -> str:
    """只认有效行：注释（如 MFAA_TAG）改动不触发重装。"""
    text = (project_root / "requirements.txt").read_text(encoding="utf-8")
    lines = (re.sub(r"(^|\s)#.*$", "", line).strip() for line in text.splitlines())
    return hashlib.sha256("\n".join(line for line in lines if line).encode("utf-8")).hexdigest()

def wanted_state(project_root: Path, target) -> str:
    return f"{requirements_fingerprint(project_root)} maafw={target or '-'}"

def normalize_version(raw: str):
    """v5.12.2 → 5.12.2；PyPI 上不会有的写法（预发布等）返回 None。"""
    version = raw.strip()
    version = version[1:] if version[:1] in ("v", "V") else version
    return version if re.fullmatch(r"\d+\.\d+\.\d+", version) else None

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def _try_lock(handle) -> bool:
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

def _unlock(handle):
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def sync_running(venv_path: Path) -> bool:
    path = venv_path / LOCK_NAME
    if not path.exists():
        return False
    with open(path, "a+") as handle:
        if not _try_lock(handle):
            return True
        _unlock(handle)
        return False

def wait_sync(venv_path: Path, timeout: int = 900):
    deadline = time.monotonic() + timeout
    while sync_running(venv_path):
        if time.monotonic() > deadline:
            _abort([f"等了 {timeout // 60} 分钟同步仍未结束，本次不启动；窗口结束后重新启动。"])
        time.sleep(0.5)
    importlib.invalidate_caches()  # 随后要读新的 dist-info、导入刚装的包

# ---------------------------------------------------------
# Agent 侧：启动时检查（runtime_environment.prepare 调用）
# ---------------------------------------------------------

def client_maafw_version():
    raw = os.environ.get(CLIENT_MAAFW_ENV, "").strip()
    if not raw:
        return None
    version = normalize_version(raw)
    if version is None:
        mfaalog.warning(f"客户端报告的框架版本 {raw!r} 对不上 PyPI 的 maafw 正式版，不跟随。")
    return version

def installed_maafw_version():
    """只读 dist-info，不加载原生库。"""
    try:
        raw = importlib.metadata.version("maafw")
    except importlib.metadata.PackageNotFoundError:
        return None
    # maafw 5.14.3 的元数据写的是 Version: v5.14.3，pip 规范化时去掉 v，这里读到的是原样
    return normalize_version(raw) or raw

def pending_changes(project_root: Path, venv_path: Path, target) -> list:
    pending = []
    if _read(venv_path / MARKER_NAME) != requirements_fingerprint(project_root):
        pending.append("requirements.txt 有更新")
    current = installed_maafw_version()
    if target and current != target:
        pending.append(f"maafw {current or '未安装'} → {target}（跟随客户端）")
    elif not target and current is None:
        pending.append("maafw 未安装")
    return pending

def sync_command(project_root: Path, target) -> str:
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" --root "{project_root}"'
    return command + (f" --maafw {target}" if target else "")

def launch_sync(project_root: Path, target) -> bool:
    """在独立窗口里同步，返回是否已拉起。Agent 随后退出，同步不受它牵连。"""
    if sys.platform != "win32":
        return False
    # 用基础解释器：venv 的 Scripts/python.exe 只是转发器，会再套一层作业，干扰同步进程的自查
    python = getattr(sys, "_base_executable", None) or sys.executable
    args = [python, "-I", str(Path(__file__).resolve()), "--root", str(project_root), "--window"]
    if target:
        args += ["--maafw", target]
    # 经 cmd 的 start 开新窗口：
    #   · argv[0] 不是 python，调试器 debugpy 不会把它注入成子调试会话；
    #   · 尽量脱离父进程所在的作业对象：客户端会在 Agent 退出后结束整个作业里的进程
    #     （debugpy 允许脱离，MFAA 不允许）。脱不开的由同步进程自查后拒绝开工。
    venv_path = get_venv_path(project_root)
    refused = venv_path / REFUSED_NAME
    refused.unlink(missing_ok=True)
    command = ["cmd", "/c", "start", SYNC_WINDOW_TITLE, *args]
    try:
        subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB) # nosec
    except OSError as e:
        mfaalog.warning(f"无法单独拉起同步窗口: {e}")
        return False
    # start 失败时无声无息，看到同步进程拿到锁才算拉起
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sync_running(venv_path):
            return True
        reason = _read(refused)
        if reason:
            refused.unlink(missing_ok=True)
            mfaalog.warning(reason)
            return False
        time.sleep(0.2)
    mfaalog.warning("同步窗口没有按时启动")
    return False

def _abort(lines):
    mfaalog.error("=" * 64)
    for line in lines:
        mfaalog.error(f"[venv] {line}")
    mfaalog.error("=" * 64)
    # 不用 SystemExit：调试器 debugpy 默认把非零 SystemExit 当未捕获异常停下，
    # 进程不退出，客户端只能干等到连接超时。此时还没导入 maa，没有要收尾的资源。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)

def ensure_venv(project_root: Path):
    venv_path = get_venv_path(project_root)
    venv_python = get_venv_executable(venv_path)
    if is_running_in_venv() and not is_running_in(venv_path):
        mfaalog.info(f"当前在外部虚拟环境中运行（{sys.prefix}），依赖由使用者自行管理。")
        return

    if not is_running_in_venv():
        # Windows 没有 exec 语义，原先用子进程重启会形成同名双层 python 进程：
        # kill 哪个都不确定，杀父进程则子进程变孤儿，继续持有 AgentServer 的 socket。
        # 故不在这里切解释器，只给出可直接粘贴的配置；环境缺了就顺手拉起同步去建。
        main_py = (project_root / "agent" / "main.py").as_posix()
        lines = ["Agent 需要用虚拟环境的解释器启动，当前用的是宿主解释器。"]
        if not venv_python.exists():
            launched = launch_sync(project_root, client_maafw_version())
            lines.append("虚拟环境还没建，" + (f"已在「{SYNC_WINDOW_TITLE}」窗口中创建。" if launched
                                         else f"请先运行: {sync_command(project_root, None)}"))
        lines += [
            "请把 interface.json 的 agent 段改为:",
            f'    "child_exec": "{venv_python.as_posix()}",',
            f'    "child_args": ["-u", "-X", "utf8=1", "{main_py}"]',
        ]
        _abort(lines)

    if sync_running(venv_path):
        mfaalog.info(f"「{SYNC_WINDOW_TITLE}」窗口正在同步，等它完成后继续启动…")
        wait_sync(venv_path)

    target = client_maafw_version()
    pending = pending_changes(project_root, venv_path, target)
    failed_before = _read(venv_path / FAILED_NAME) == wanted_state(project_root, target)
    if pending and not failed_before:
        command = sync_command(project_root, target)
        if not launch_sync(project_root, target):
            _abort([f"开发环境需要同步：{'；'.join(pending)}", f"请运行 {command}，完成后重新启动。"])
        mfaalog.info(f"开发环境需要同步：{'；'.join(pending)}。已在「{SYNC_WINDOW_TITLE}」窗口中进行，"
                     f"完成后自动继续启动；客户端若先报连接失败（VS Code 插件按 maa.agentTimeout 计时），"
                     f"等窗口关闭后重新启动即可。")
        wait_sync(venv_path)
        pending = pending_changes(project_root, venv_path, target)
        failed_before = _read(venv_path / FAILED_NAME) == wanted_state(project_root, target)
        if pending and not failed_before:
            # 同步进程没留下结果就没了（被结束等），不再自动重拉，免得反复启动反复失败
            _abort([f"同步没有完成：{'；'.join(pending)}", f"请运行 {command}，完成后重新启动。"])
        if not pending:
            mfaalog.info("✅ 开发环境已同步，继续启动")
            return

    if not pending:
        mfaalog.debug("开发环境与客户端一致")
        return
    mfaalog.warning(f"开发环境未同步：{'；'.join(pending)}。上次同步失败，本次沿用现有环境；"
                    f"处理后运行 {sync_command(project_root, target)} 重试。")

# ---------------------------------------------------------
# 同步侧：作为脚本运行，只用标准库
# ---------------------------------------------------------

def _run(command, **kwargs) -> bool:
    print("$", " ".join(str(part) for part in command), flush=True)
    try:
        subprocess.check_call(command, **kwargs) # nosec
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"❌ 失败: {e}", flush=True)
        return False

def _pip(python: Path, *args: str) -> bool:
    # requirements.txt 带中文注释，系统代码页下 pip 会解码失败
    env = {**os.environ, "PYTHONUTF8": "1"}
    return _run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                 "-i", PIP_INDEX, *args], env=env)

def _installed_maafw(python: Path):
    result = subprocess.run( # nosec
        [str(python), "-c", "import importlib.metadata as m; print(m.version('maafw'))"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    return normalize_version(raw) or raw  # 同 installed_maafw_version，去掉元数据里的 v

def find_preferred_python() -> str:
    if sys.platform == "win32":
        try:
            cmd = ["py", f"-{PREFERRED_PYTHON_VERSION}", "-c", "import sys; print(sys.executable)"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True) # nosec
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    else:
        path = shutil.which(f"python{PREFERRED_PYTHON_VERSION}")
        if path:
            return path
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    print(f"⚠️ 没找到 Python {PREFERRED_PYTHON_VERSION}，改用当前解释器 ({current})。")
    return sys.executable

def check_existing_venv_version(venv_path: Path):
    for line in _read(venv_path / "pyvenv.cfg").splitlines():
        if line.startswith("version"):
            version = line.split("=")[1].strip()
            if not version.startswith(PREFERRED_PYTHON_VERSION):
                print(f"⚠️ 虚拟环境是 Python {version}，首选 {PREFERRED_PYTHON_VERSION}；"
                      f"遇到兼容问题可删除 .venv 重建。")
            return

def _in_kill_on_close_job() -> bool:
    """当前进程所在作业关闭时会结束它的话，同步就可能停在卸了旧版、没装新版的半路。"""
    if sys.platform != "win32":
        return False
    import ctypes
    import ctypes.wintypes as w

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    info = BasicLimit()
    # 句柄传 NULL 即查本进程的直接作业；不在作业里时调用失败
    if not ctypes.windll.kernel32.QueryInformationJobObject(None, 2, ctypes.byref(info), ctypes.sizeof(info), None):
        return False
    return bool(info.LimitFlags & 0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

def _sync_locked(project_root: Path, venv_path: Path, target) -> bool:
    python = get_venv_executable(venv_path)
    if python.exists():
        check_existing_venv_version(venv_path)
    else:
        base = find_preferred_python()
        print(f"创建虚拟环境 {venv_path}（基础解释器 {base}）", flush=True)
        if not _run([base, "-m", "venv", str(venv_path)]):
            return False

    fingerprint = requirements_fingerprint(project_root)
    marker = venv_path / MARKER_NAME
    if _read(marker) == fingerprint:
        print("requirements.txt 无变化", flush=True)
    else:
        if not _pip(python, "-r", str(project_root / "requirements.txt")):
            return False
        marker.write_text(fingerprint, encoding="utf-8")

    current = _installed_maafw(python)
    if target and current != target:
        print(f"maafw {current or '未安装'} → {target}", flush=True)
        return _pip(python, f"maafw=={target}")
    if not target and current is None:
        return _pip(python, f"maafw{FALLBACK_MAAFW_SPEC}")
    print(f"maafw {current} 无需变更", flush=True)
    return True

def sync(project_root: Path, target=None) -> bool:
    """建 venv、装依赖，全程持锁。失败时记下目标状态，Agent 据此不再为同一目标反复拦启动。"""
    venv_path = get_venv_path(project_root)
    venv_path.mkdir(parents=True, exist_ok=True)
    with open(venv_path / LOCK_NAME, "a+") as lock:
        if not _try_lock(lock):
            print("另一个同步正在进行，本次退出。")
            return False
        try:
            ok = _sync_locked(project_root, venv_path, target)
            failed = venv_path / FAILED_NAME
            if ok:
                failed.unlink(missing_ok=True)
            else:
                failed.write_text(wanted_state(project_root, target), encoding="utf-8")
            return ok
        finally:
            _unlock(lock)

def main(argv=None) -> int:
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="同步开发侧 .venv；首次搭环境也用它。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help="项目根目录")
    parser.add_argument("--maafw", help="目标版本，如 5.12.2；不给则已装的保持、未装的按兼容范围装")
    parser.add_argument("--window", action="store_true", help=argparse.SUPPRESS)  # 由 Agent 拉起的独立窗口
    args = parser.parse_args(argv)

    target = None
    if args.maafw:
        target = normalize_version(args.maafw)
        if target is None:
            parser.error(f"--maafw {args.maafw} 不是 X.Y.Z 形式的正式版")
    root = args.root.resolve()
    if args.window and _in_kill_on_close_job():
        # 拉起 Agent 的客户端会连带结束本进程（MFAA 即如此），什么都不动，交回 Agent 提示手动运行
        venv_path = get_venv_path(root)
        venv_path.mkdir(parents=True, exist_ok=True)
        (venv_path / REFUSED_NAME).write_text("客户端会连带结束 Agent 拉起的进程，同步不能在后台进行。", encoding="utf-8")
        return 3
    ok = sync(root, target)
    print("✅ 开发环境已同步，可以重新启动 Agent。" if ok else "❌ 同步没有完成，见上方输出。", flush=True)
    if args.window:
        if ok:
            time.sleep(5)
        else:
            input("按回车关闭窗口")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
