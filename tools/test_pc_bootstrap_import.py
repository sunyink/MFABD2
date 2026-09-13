"""Verify the standalone pretask imports under isolated Python, without running it."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BootstrapImportTests(unittest.TestCase):
    def test_isolated_interpreter_from_unrelated_working_directory(self):
        script = ROOT / "agent/pc_bootstrap.py"
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
                [sys.executable, "-I", "-B", "-X", "utf8=1", "-c", code, str(script)],
                cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["startup"]), script.parent.resolve() / "startup/common.py")


if __name__ == "__main__":
    unittest.main()
