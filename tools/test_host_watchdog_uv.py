"""离线测试：Agent 经 uv run 启动时，uv 这一层不能被宿主守护当成宿主。

不导入 maa，Windows 与类 Unix 都能跑：

    python tools/test_host_watchdog_uv.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from utils import host_watchdog as hw  # noqa: E402


class WindowsIntermediate(unittest.TestCase):
    def test_uv_is_a_wrapper(self):
        for image in ("uv.exe", "UV.EXE", "uvw.exe", "python.exe", "pythonw.exe"):
            self.assertTrue(hw.HostWatchdog._is_intermediate(image, ""), image)

    def test_real_hosts_are_not_wrappers(self):
        for image in ("Code.exe", "MFAAvalonia.exe", "pwsh.exe", "uvicorn.exe", ""):
            self.assertFalse(hw.HostWatchdog._is_intermediate(image, ""), image)


class ProcStat(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(hw._parse_proc_stat("123 (uv) S 45 123 45 0 -1"), (45, "uv"))

    def test_name_with_spaces_and_parentheses(self):
        self.assertEqual(hw._parse_proc_stat("77 (my (odd) name) R 12 77 12 0"), (12, "my (odd) name"))


class PosixChain(unittest.TestCase):
    """用假的进程表代替 /proc 与 ps，只测「谁算宿主、宿主没了能不能发现」。"""

    def watchdog(self, table, ppid):
        self.table = table
        original = hw._posix_process
        hw._posix_process = lambda pid: self.table.get(pid)
        self.addCleanup(setattr, hw, "_posix_process", original)
        dog = object.__new__(hw.HostWatchdog)
        dog.ppid, dog.targets, dog.exited_target, dog._kernel32 = ppid, [], None, None
        dog._posix_wrappers = dog._resolve_posix_wrappers()
        return dog

    def test_parent_is_the_host(self):
        dog = self.watchdog({20: (1, "MFAAvalonia")}, ppid=20)
        self.assertEqual(dog._posix_wrappers, [])
        self.assertFalse(dog._posix_host_gone())

    def test_uv_sits_between_agent_and_host(self):
        dog = self.watchdog({30: (20, "uv"), 20: (1, "MFAAvalonia")}, ppid=30)
        self.assertEqual(dog._posix_wrappers, [(30, 20)])
        self.assertEqual(dog.describe(), "30(uv) / 20(host)")
        self.assertFalse(dog._posix_host_gone())

    def test_host_exit_reparents_uv(self):
        dog = self.watchdog({30: (20, "uv"), 20: (1, "MFAAvalonia")}, ppid=30)
        self.table[30] = (1, "uv")  # 宿主退出，uv 被过继；本进程的父进程号此时并没有变
        self.assertTrue(dog._posix_host_gone())
        self.assertEqual(dog.exited_target.pid, 20)

    def test_uv_exit_counts_as_gone(self):
        dog = self.watchdog({30: (20, "uv"), 20: (1, "MFAAvalonia")}, ppid=30)
        del self.table[30]
        self.assertTrue(dog._posix_host_gone())

    def test_nested_uv(self):
        dog = self.watchdog({40: (30, "uv"), 30: (20, "uv"), 20: (1, "code")}, ppid=40)
        self.assertEqual(dog._posix_wrappers, [(40, 30), (30, 20)])
        self.table[30] = (1, "uv")
        self.assertTrue(dog._posix_host_gone())


if __name__ == "__main__":
    unittest.main()
