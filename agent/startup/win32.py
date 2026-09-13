"""Small native adapter. Imported only for actual Win32 preparation."""

import ctypes as C
import ntpath
import os
import re
from ctypes import wintypes as W

from .common import PreparationError

GAME_EXE = "browndust ii.exe"
STARTER_EXE = "browndust2starter.exe"
GAME_CLASS = "UnityWndClass"
GAME_TITLE = "BrownDust II"
GAME_URI = "browndust2:games/10000001?usn=0"


class ProcessEntry(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                ("th32ProcessID", W.DWORD), ("th32DefaultHeapID", C.c_size_t),
                ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", W.LONG),
                ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


class WindowsAPI:
    def __init__(self):
        if os.name != "nt":
            raise PreparationError("PC 启动准备仅支持 Windows")
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.enum_callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        signatures = [
            (self.user, "EnumWindows", W.BOOL, [self.enum_callback, W.LPARAM]),
            (self.user, "IsWindow", W.BOOL, [W.HWND]),
            (self.user, "IsWindowVisible", W.BOOL, [W.HWND]),
            (self.user, "IsIconic", W.BOOL, [W.HWND]),
            (self.user, "IsZoomed", W.BOOL, [W.HWND]),
            (self.user, "GetWindowThreadProcessId", W.DWORD, [W.HWND, C.POINTER(W.DWORD)]),
            (self.user, "GetClassNameW", C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            (self.user, "GetWindowTextW", C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            (self.user, "GetClientRect", W.BOOL, [W.HWND, C.POINTER(W.RECT)]),
            (self.user, "GetWindowRect", W.BOOL, [W.HWND, C.POINTER(W.RECT)]),
            (self.user, "ShowWindowAsync", W.BOOL, [W.HWND, C.c_int]),
            (self.user, "SetWindowPos", W.BOOL, [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]),
            (self.user, "PostMessageW", W.BOOL, [W.HWND, W.UINT, W.WPARAM, W.LPARAM]),
            (self.user, "GetWindowLongW", W.LONG, [W.HWND, C.c_int]),
            (self.user, "GetLayeredWindowAttributes", W.BOOL, [W.HWND, C.POINTER(W.DWORD), C.POINTER(W.BYTE), C.POINTER(W.DWORD)]),
            (self.user, "SetThreadDpiAwarenessContext", W.HANDLE, [W.HANDLE]),
            (self.kernel, "OpenProcess", W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            (self.kernel, "CloseHandle", W.BOOL, [W.HANDLE]),
            (self.kernel, "QueryFullProcessImageNameW", W.BOOL, [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]),
            (self.kernel, "CreateToolhelp32Snapshot", W.HANDLE, [W.DWORD, W.DWORD]),
            (self.kernel, "Process32FirstW", W.BOOL, [W.HANDLE, C.POINTER(ProcessEntry)]),
            (self.kernel, "Process32NextW", W.BOOL, [W.HANDLE, C.POINTER(ProcessEntry)]),
        ]
        for dll, name, result, args in signatures:
            func = getattr(dll, name)
            func.restype, func.argtypes = result, args
        self.previous_dpi = None

    def __enter__(self):
        # Coordinate calls on this thread use physical pixels, without changing
        # the UI/controller process or the awareness of other agent threads.
        self.previous_dpi = self.user.SetThreadDpiAwarenessContext(C.c_void_p(-4))
        if not self.previous_dpi:
            raise PreparationError("无法启用物理像素坐标模式")
        return self

    def __exit__(self, *_):
        if self.previous_dpi:
            self.user.SetThreadDpiAwarenessContext(self.previous_dpi)
            self.previous_dpi = None

    def _processes(self):
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        processes = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = C.sizeof(entry)
            more = self.kernel.Process32FirstW(snapshot, C.byref(entry))
            if not more:
                raise C.WinError(C.get_last_error())
            while more:
                processes[entry.th32ProcessID] = entry.szExeFile.lower()
                more = self.kernel.Process32NextW(snapshot, C.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        return processes

    def process_path(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buf, size = C.create_unicode_buffer(32768), W.DWORD(32768)
            if self.kernel.QueryFullProcessImageNameW(handle, 0, buf, C.byref(size)):
                return buf.value
            return ""
        finally:
            self.kernel.CloseHandle(handle)

    def _pid(self, hwnd):
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return pid.value

    def _class(self, hwnd):
        buf = C.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, buf, len(buf))
        return buf.value

    def _title(self, hwnd):
        buf = C.create_unicode_buffer(512)
        self.user.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def is_game(self, hwnd):
        return bool(self.user.IsWindow(hwnd) and self._class(hwnd) == GAME_CLASS
                    and ntpath.basename(self.process_path(self._pid(hwnd))).lower() == GAME_EXE)

    def scan(self):
        processes = self._processes()
        games, dialogs = [], []

        @self.enum_callback
        def collect(hwnd, _):
            if not self.user.IsWindowVisible(hwnd):
                return True
            exe = processes.get(self._pid(hwnd), "")
            # Match the UI's main-window filter before handing control back;
            # a transient Unity window with no final title is not connectable.
            if exe == GAME_EXE and self.is_game(hwnd) and self._title(hwnd) == GAME_TITLE:
                games.append(hwnd)
            elif exe == STARTER_EXE:
                dialogs.append(self._title(hwnd) or self._class(hwnd))
            return True

        if not self.user.EnumWindows(collect, 0):
            raise C.WinError(C.get_last_error())
        running = any(exe in (GAME_EXE, STARTER_EXE) for exe in processes.values())
        return games, running, tuple(sorted(set(dialogs)))

    def launch(self):
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"browndust2\shell\open\command") as key:
                command = winreg.QueryValueEx(key, "")[0]
        except OSError as exc:
            raise PreparationError("未找到官方 browndust2 启动入口，请先安装 PC 独立版并手动启动一次") from exc
        match = re.match(r'^\s*(?:"([^"]+)"|(\S+))', os.path.expandvars(command))
        executable = next((part for part in match.groups() if part), "") if match else ""
        if ntpath.basename(executable).lower() != STARTER_EXE or not os.path.isfile(executable):
            raise PreparationError("官方启动器注册路径无效，请修复 PC 独立版安装")
        os.startfile(GAME_URI)

    def restore(self, hwnd):
        if self.user.IsIconic(hwnd) or self.user.IsZoomed(hwnd):
            self.user.ShowWindowAsync(hwnd, 9)
            return False
        return True

    def minimized(self, hwnd):
        return bool(self.user.IsIconic(hwnd))

    def pseudo_minimized(self, hwnd):
        style = self.user.GetWindowLongW(hwnd, -20)
        if style & 0x80020 != 0x80020:
            return False
        color, alpha, flags = W.DWORD(), W.BYTE(), W.DWORD()
        return bool(self.user.GetLayeredWindowAttributes(hwnd, C.byref(color), C.byref(alpha), C.byref(flags))
                    and flags.value & 2 and alpha.value == 0)

    def minimize(self, hwnd):
        if not self.user.IsWindow(hwnd):
            raise PreparationError("最小化前游戏窗口已失效")
        self.user.ShowWindowAsync(hwnd, 6)

    def fullscreen(self, hwnd):
        return not bool(self.user.GetWindowLongW(hwnd, -16) & 0x00C00000)

    def client_size(self, hwnd):
        rect = W.RECT()
        if not self.user.GetClientRect(hwnd, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        return rect.right - rect.left, rect.bottom - rect.top

    def resize_client(self, hwnd, target):
        width, height = self.client_size(hwnd)
        rect = W.RECT()
        if not self.user.GetWindowRect(hwnd, C.byref(rect)):
            raise C.WinError(C.get_last_error())
        outer_width = rect.right - rect.left + target[0] - width
        outer_height = rect.bottom - rect.top + target[1] - height
        # Async positioning avoids blocking on the game's UI thread.
        if not self.user.SetWindowPos(hwnd, None, 0, 0, outer_width, outer_height, 0x4000 | 0x16):
            raise C.WinError(C.get_last_error())

    def exit_fullscreen(self, hwnd):
        # Unity's Alt+Enter handling; never rewrite Unity window styles.
        for msg, param in ((0x104, 1 | (0x1C << 16) | (1 << 29)),
                           (0x105, 1 | (0x1C << 16) | (1 << 29) | (3 << 30))):
            if not self.user.PostMessageW(hwnd, msg, 0x0D, param):
                raise C.WinError(C.get_last_error())
