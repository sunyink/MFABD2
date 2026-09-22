"""Verify the standalone pretask imports under isolated Python, without running it.

注意：这里绝不能真的跑 main()。在 Windows 上它会走到 api.launch()，那是真的去
拉起游戏。需要验证 main 的收场行为时一律替换掉 _run。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def isolated(code, *arguments):
    """在隔离解释器里跑一段代码，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8=1", "-c", code, *[str(a) for a in arguments]],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )


class BootstrapImportTests(unittest.TestCase):
    script = ROOT / "agent/pc_bootstrap.py"

    def test_isolated_interpreter_from_unrelated_working_directory(self):
        code = """
import json
import runpy
import sys
from pathlib import Path
script = Path(sys.argv[1]).resolve()
assert str(script.parent) not in sys.path
runpy.run_path(str(script), run_name='bootstrap_import_test')
import startup.common
assert Path(startup.common.__file__).resolve() == script.parent / 'startup/common.py'
assert 'startup.win32' not in sys.modules
assert 'utils.host_watchdog' not in sys.modules
assert 'maa' not in sys.modules
print(json.dumps({'startup': str(Path(startup.common.__file__).resolve())}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8=1", "-c", code, str(self.script)],
                cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["startup"]),
                         self.script.parent.resolve() / "startup/common.py")

    def test_boot_log_records_cwd_argv_and_interpreter(self):
        # 这一行是线上排查「pretask 到底有没有起来」的唯一读数，必须自己不会失败。
        code = """
import runpy
import sys
from pathlib import Path
script, workspace = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
mod = runpy.run_path(str(script), run_name='bootstrap_import_test')
# run_name 不是 __main__，所以导入期绝不能落盘，否则纯 import 测试会污染仓库。
assert not (workspace / 'pc_bootstrap.log').exists()
boot = mod['_boot_log']
boot.__globals__['AGENT_DIR'] = workspace / 'agent'
assert boot(workspace, note='probe') == workspace
text = (workspace / 'pc_bootstrap.log').read_text(encoding='utf-8')
for marker in ('[boot]', 'cwd=', 'argv=', 'exe=', 'file=', 'note=probe'):
    assert marker in text, marker
# 首选目录不可写时退到 AGENT_DIR/../debug，并且绝不抛异常。
blocked = workspace / 'blocked'
blocked.write_text('not a directory', encoding='utf-8')
assert boot(blocked) == workspace / 'debug'
print('ok')
"""
        with tempfile.TemporaryDirectory() as directory:
            result = isolated(code, self.script, directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_main_never_propagates_and_logs_the_reason(self):
        # pretask 永远以 0 退出：协议对退出码没有规定，赌不得上层软件的行为。
        code = """
import runpy
import sys
from pathlib import Path
script, workspace = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
mod = runpy.run_path(str(script), run_name='bootstrap_import_test')
globals_ = mod['_boot_log'].__globals__
globals_['AGENT_DIR'] = workspace / 'agent'

def boom(report):
    raise RuntimeError('boom')

globals_['_run'] = boom
globals_['main']()
if sys.platform == 'win32':
    text = (workspace / 'debug' / 'pc_bootstrap.log').read_text(encoding='utf-8')
    assert 'boom' in text, text
    assert '不阻断任务队列' in text, text
print('ok')
"""
        with tempfile.TemporaryDirectory() as directory:
            result = isolated(code, self.script, directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
