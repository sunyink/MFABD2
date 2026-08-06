# -*- coding: utf-8 -*-
"""宿主存活守护。

【为什么需要它】
MaaFramework 的 AgentServer 侧接收超时恒为 `milliseconds::max()`
(source/include/MaaAgent/Transceiver.h:118)，`Transceiver::poll()` 里
`if (elapsed > timeout_)` 因此永远为假，`recv()` 会以 1 秒为粒度永远轮询下去。
退出 `request_msg_loop` 只有一条路：UI 侧发来 ShutDownRequest。而 UI 被强杀、
崩溃、或调试会话被拔掉时，`AgentClient::disconnect()` 根本不执行，那条消息永远
不会来 —— `AgentServer.join()` 于是永不返回，Agent 进程永久驻留，仍持有 socket。

MaaAgentServerAPI.h 只暴露 StartUp / ShutDown / Join / Detach，**没有任何 timeout
设置接口**（`set_timeout` 仅 AgentClient 侧调用），所以这一层只能由我们自己补。

【为什么不能用 os.getppid()】
Windows 上 `.venv\\Scripts\\python.exe` **不是解释器本体**，是 CPython 自带的
venv redirector（源码 PC/launcher.c，产物 venvlauncher.exe）。它读 pyvenv.cfg 的
`home=` 找到真解释器，`CreateProcessW` 原样转发命令行，设 `__PYVENV_LAUNCHER__`
让真解释器把 `sys.executable` 认成 venv 路径，然后 `WaitForSingleObject(INFINITE)`
干等子进程并转发退出码。Python 3.7.2+ 在 Windows 上 `python -m venv` 建出来的环境
一律如此。

于是只要 `child_exec` 指向 `.venv/Scripts/python.exe`（interface.json 现在正是如此），
真 Agent 进程的父**永远是这个 launcher**，而不是 UI：
    MFAAvalonia / pwsh  →  venv launcher  →  agent(本进程)
拿 `getppid()` 当宿主会构成自指死锁 —— launcher 在等 agent 退出，watchdog 在等
launcher 退出，`WaitForSingleObject` 永不 signaled，守护 100% 失效。
（实测：agent 是 Px7144，却打印「宿主守护已启动 (UI pid=5820)」，5820 就是 launcher。）

【实现要点】
Windows 上启动时做一次进程快照，从父进程沿祖先链向上：
  · 途中每一层 python（launcher 及任何 python 包装）都**纳入监视**并继续向上；
  · 第一个非 python 祖先即真正的宿主（MFAAvalonia / pwsh / Code.exe），纳入监视后停止。
对选中的目标各持一个 `OpenProcess(SYNCHRONIZE)` 句柄，用 `WaitForMultipleObjects`
等**任意一个**退出。句柄绑定具体内核对象，PID 被复用也不会误判；目标一退出等待立即
返回，不必等到下一个轮询周期。

向上遍历设了深度上限与创建时间校验，避免 pid 复用时一路走到 `services.exe`
这类杀了要连坐的进程上。**深度上限别照某条链路的长度来定** —— 链长取决于启动方式：
`child_exec`（pwsh → venv launcher → agent）只要 3 跳，而 VSCode 的 `debug_session`
要走 6 跳才到宿主（debugpy 是 adapter → launcher → 目标 三级，每级各套一层
venv launcher）。POSIX 上没有 redirector 结构（`os.execv` 就地替换），
仍退化为 `getppid()` 变化检测。

一个目标都没拿到时 `available` 为 False，调用方应回退到原来的阻塞 join，
行为不会比现状更差。
"""

import os
import sys
import time

from . import mfaalog

# --- Windows API 常量 ---
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
_TH32CS_SNAPPROCESS = 0x00000002
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
# 其余返回值(WAIT_FAILED=0xFFFFFFFF、WAIT_ABANDONED 等)一律按「句柄失效 → 宿主已没」处理

# 祖先链向上遍历的硬上限。**别按某条具体链路的长度来定** —— 链长取决于启动方式，
# 实测三种：MFAAvalonia 直起 2 跳、child_exec 经 pwsh 3 跳、VSCode debug_session
# 足足 6 跳（debugpy 是 adapter→launcher→目标 三级，每级各套一层 venv launcher）。
# 原值 4 是照 child_exec 定的，debug_session 下够不到宿主。这里只作「跑飞兜底」，
# pid 复用另有创建时间校验做精确防护，不靠这个数。
_MAX_ANCESTOR_DEPTH = 16
# **中间层**(python 包装层)的监视数上限。宿主不受此限，见 _resolve_targets ——
# 中间层监视按 §12.5 只是无害的冗余(kill launcher 会被其 Job Object 连坐)，
# 而宿主是守护存在的唯一理由，绝不能被冗余项挤掉。
# WaitForMultipleObjects 的 nCount 上限是 MAXIMUM_WAIT_OBJECTS(64)，留足余量。
_MAX_INTERMEDIATE_TARGETS = 16

# 视作「中间层」的映像名：命中则继续向上找真宿主
_PYTHON_IMAGES = ("python.exe", "pythonw.exe", "python3.exe")


def _same_path(a: str, b: str) -> bool:
    """Windows 路径等价比较（大小写不敏感 + 分隔符归一）。"""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except Exception:
        return False


class _WatchTarget:
    """一个被监视的进程。role 仅用于日志，便于一眼看出链路结构。"""

    __slots__ = ("pid", "image", "role", "handle")

    def __init__(self, pid: int, image: str, role: str, handle=None):
        self.pid = pid
        self.image = image or "?"
        self.role = role
        self.handle = handle

    def __str__(self) -> str:
        return f"{self.pid}({self.image},{self.role})"


class HostWatchdog:
    """监视宿主进程链是否有任一环退出。"""

    def __init__(self) -> None:
        self.ppid = 0
        self.available = False
        self.targets = []           # List[_WatchTarget]
        self.exited_target = None   # 触发退出判定的那个目标，供调用方写日志
        self._kernel32 = None

        try:
            self.ppid = os.getppid()
        except OSError as e:
            mfaalog.warning(f"[Watchdog] 无法获取父进程 PID: {e}")
            return

        if self.ppid <= 0:
            mfaalog.warning(f"[Watchdog] 父进程 PID 非法 ({self.ppid})，守护不可用")
            return

        if sys.platform == "win32":
            self._init_windows()
        else:
            # POSIX 下 getppid() 变化即可判定，无需额外资源
            self.available = True

    def describe(self) -> str:
        """给日志用的一行描述。"""
        if not self.targets:
            return f"ppid={self.ppid}(未解析)"
        return " / ".join(str(t) for t in self.targets)

    # ------------------------------------------------------------------
    # Windows
    # ------------------------------------------------------------------
    def _init_windows(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
            kernel32.WaitForMultipleObjects.argtypes = [
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            self._kernel32 = kernel32

            table = self._snapshot(kernel32)
            if not table:
                mfaalog.warning("[Watchdog] 进程快照失败，将回退为阻塞等待")
                return

            self._resolve_targets(kernel32, table)

            if not self.targets:
                mfaalog.warning(
                    f"[Watchdog] 未能解析出可监视的宿主 (ppid={self.ppid})，"
                    "将回退为阻塞等待，宿主异常退出时本进程可能残留"
                )
                return

            # 只监视到 python 包装层、没走到真宿主时必须喊出来。原实现在这里静默
            # 收场，日志只说「监视了 N 个 python 层」，看着一切正常，实际守护已经
            # 对宿主失效 —— §12 那次栽的就是「日志不说谎但也不说实话」。
            if not any(t.role == "host" for t in self.targets):
                mfaalog.warning(
                    f"[Watchdog] 祖先链已走到上限仍未找到非 python 宿主，当前只监视 "
                    f"{self.describe()}。这些包装层若先于宿主退出仍能感知，但宿主"
                    "自身异常退出时可能感知不到 —— 链路确实更深就调高 _MAX_ANCESTOR_DEPTH"
                )

            self.available = True
        except Exception as e:
            mfaalog.warning(f"[Watchdog] Windows 守护初始化失败: {e}")

    @staticmethod
    def _snapshot(kernel32) -> dict:
        """一次 Toolhelp 快照拿到全表 {pid: (ppid, 映像名)}。纯 kernel32，不引入 psutil。"""
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                # ULONG_PTR：64 位下是 8 字节，用 c_size_t 才不会错位
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

        invalid = ctypes.c_void_p(-1).value
        snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == invalid:
            mfaalog.debug(
                f"[Watchdog] CreateToolhelp32Snapshot 失败 (err={ctypes.get_last_error()})"
            )
            return {}

        table = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID),
                    entry.szExeFile,
                )
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)

        return table

    def _resolve_targets(self, kernel32, table: dict) -> None:
        """从父进程沿祖先链向上，选出要监视的目标。

        规则：途中每一层 python 都纳入监视并继续向上，第一个非 python 祖先即宿主，
        纳入后停止。这样 kill 链路上任意一环，Agent 都会收敛退出。

        中间层超过 _MAX_INTERMEDIATE_TARGETS 后**只是不再纳入监视，仍继续向上找宿主** ——
        原实现在这里直接 break，于是 debug_session 那条 6 跳链路上三个 debugpy 包装层
        就把名额占满了，宿主(Code.exe)根本走不到，守护退化成「只监视 debugpy 内部」。
        """
        self_pid = os.getpid()
        self_create = self._create_time(kernel32, self_pid)

        seen = {self_pid}
        cur = self.ppid
        depth = 0
        intermediates = 0

        while cur and cur not in seen and depth < _MAX_ANCESTOR_DEPTH:
            seen.add(cur)
            depth += 1

            info = table.get(cur)
            if info is None:
                mfaalog.debug(f"[Watchdog] 祖先 pid={cur} 不在快照中（已退出？），停止向上")
                break
            next_ppid, image_name = info

            handle = kernel32.OpenProcess(
                _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, cur
            )
            if not handle:
                # 降级：只要能 Wait 就够，SYNCHRONIZE 是最低要求
                handle = kernel32.OpenProcess(_SYNCHRONIZE, False, cur)
            if not handle:
                import ctypes

                mfaalog.warning(
                    f"[Watchdog] OpenProcess 失败 (pid={cur}, image={image_name}, "
                    f"err={ctypes.get_last_error()})，该层无法监视"
                )
                break

            # pid 复用防护：祖先的创建时间不可能晚于本进程
            create = self._create_time(kernel32, cur, handle)
            if self_create and create and create > self_create:
                mfaalog.warning(
                    f"[Watchdog] pid={cur}({image_name}) 创建时间晚于本进程，"
                    "判定为 PID 复用，停止向上"
                )
                kernel32.CloseHandle(handle)
                break

            image_path = self._image_path(kernel32, handle)

            if self._is_intermediate(image_name, image_path):
                if intermediates < _MAX_INTERMEDIATE_TARGETS:
                    # 判据 1 命中时说明它就是启动我的那个 venv launcher
                    role = "venv-launcher" if _same_path(image_path, sys.executable) else "python"
                    self.targets.append(_WatchTarget(cur, image_name, role, handle))
                    intermediates += 1
                else:
                    # 名额用尽就不监视这一层，但句柄必须还回去，否则每跳泄漏一个
                    kernel32.CloseHandle(handle)
                cur = next_ppid
                continue

            self.targets.append(_WatchTarget(cur, image_name, "host", handle))
            break

    @staticmethod
    def _is_intermediate(image_name: str, image_path: str) -> bool:
        """判断这一层是不是「包装层」，需要继续向上找真宿主。

        判据 1（强）：它的映像就是我自称的解释器 —— 只有 venv redirector 会这样，
                     因为 __PYVENV_LAUNCHER__ 把我的 sys.executable 改写成了它的路径。
        判据 2（兜底）：映像名是 python.exe / pythonw.exe。
        """
        if _same_path(image_path, sys.executable):
            return True
        return (image_name or "").lower() in _PYTHON_IMAGES

    @staticmethod
    def _create_time(kernel32, pid: int, handle=None):
        """返回进程创建时间（100ns 计数）。拿不到返回 None。"""
        import ctypes
        from ctypes import wintypes

        own = handle is None
        if own:
            handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
        try:
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kern = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kern),
                ctypes.byref(user),
            ):
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        except Exception:
            return None
        finally:
            if own:
                kernel32.CloseHandle(handle)

    @staticmethod
    def _image_path(kernel32, handle) -> str:
        """QueryFullProcessImageNameW 拿完整映像路径。拿不到返回空串。"""
        import ctypes
        from ctypes import wintypes

        try:
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def host_exited(self, timeout: float) -> bool:
        """阻塞至多 timeout 秒等待宿主链中任一环退出。

        Returns
        -------
        bool
            True  = 有监视目标已退出（宿主没了），具体是哪个见 `exited_target`
            False = 超时，全都还在
        """
        if not self.available:
            time.sleep(timeout)
            return False

        if self.targets and self._kernel32 is not None:
            return self._wait_windows(timeout)

        # POSIX: 轮询 getppid()。父进程退出后子进程被 reparent，ppid 必然改变。
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.getppid() != self.ppid:
                    return True
            except OSError:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.5, remaining))

    def _wait_windows(self, timeout: float) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = self._kernel32
        alive = [t for t in self.targets if t.handle]
        if not alive or kernel32 is None:
            return True

        count = len(alive)
        arr = (wintypes.HANDLE * count)(*[t.handle for t in alive])
        ret = kernel32.WaitForMultipleObjects(count, arr, False, int(timeout * 1000))

        if _WAIT_OBJECT_0 <= ret < _WAIT_OBJECT_0 + count:
            self.exited_target = alive[ret - _WAIT_OBJECT_0]
            return True
        if ret == _WAIT_TIMEOUT:
            return False

        # WAIT_FAILED 等异常返回：句柄已失效，按宿主已消失处理（保守方向是退出）
        mfaalog.warning(
            f"[Watchdog] WaitForMultipleObjects 返回异常值 {ret:#x} "
            f"(err={ctypes.get_last_error()})，按宿主已退出处理"
        )
        return True

    def close(self) -> None:
        if self._kernel32 is None:
            self.targets = []
            return
        for t in self.targets:
            if t.handle:
                try:
                    self._kernel32.CloseHandle(t.handle)
                except Exception:
                    pass
                t.handle = None
        self.targets = []


def cleanup_socket_file(socket_id: str) -> None:
    """删除 IPC 模式下残留的 socket 文件。

    正常退出时由 Transceiver 的析构负责删除，但宿主失联后我们只能 os._exit()
    （见 main.py 的说明），析构不会跑，所以在这里补一刀。
    纯数字的 identifier 是 TCP 模式，没有文件。
    """
    if not socket_id or socket_id.isdigit():
        return
    try:
        from pathlib import Path

        # 路径与 AgentCommon/Transceiver.cpp 的 temp_directory() 保持一致
        base = Path("C:/Temp") if sys.platform == "win32" else Path("/tmp")
        sock = base / f"maafw-agent-{socket_id}.sock"
        if sock.exists():
            sock.unlink()
            mfaalog.debug(f"[Watchdog] 已清理残留 socket: {sock}")
    except Exception as e:
        mfaalog.debug(f"[Watchdog] 清理 socket 文件失败（不影响退出）: {e}")
