"""Host-contract and save-data regression checks; never touches real user saves.

Run: python tools/verify_android_runtime.py
These tests simulate host environments, not an Android device or native callback.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import runtime_environment as runtime
from utils.persistent_store import PersistentStore


class AndroidRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(runtime.platform, "system", return_value="Linux"))
        self.store = type("IsolatedStore", (PersistentStore,), {"_initialized": False})
        for method in ("info", "warning", "error"):
            self.enterContext(patch(f"utils.persistent_store.logger.{method}"))

    def libraries(self):
        path = self.root / "native"
        path.mkdir()
        for name in ("libMaaFramework.so", "libMaaAgentServer.so"):
            (path / name).touch()
        return path

    def test_host_marks_android_even_when_python_reports_linux(self):
        os.environ["PI_CLIENT_NAME"] = "MaaFwApp"
        self.assertTrue(runtime.is_android())

    def test_native_path_is_preserved(self):
        native = self.libraries()
        os.environ["MAAFW_BINARY_PATH"] = str(native)
        self.assertEqual(runtime.android_library_dir(), native)
        self.assertEqual(os.environ["MAAFW_BINARY_PATH"], str(native))

    def test_android_bootstrap_skips_desktop_venv_with_requirements_present(self):
        import runpy
        import types
        native = self.libraries()
        os.environ.update(PI_CLIENT_NAME="MaaFwApp", MAAFW_BINARY_PATH=str(native))
        modules = {name: types.ModuleType(name) for name in (
            "maa", "maa.agent", "maa.agent.agent_server", "maa.toolkit",
            "action", "recognition", "fishing_agent",
        )}
        modules["maa.agent.agent_server"].AgentServer = object
        modules["maa.toolkit"].Toolkit = object
        with patch.dict(sys.modules, modules), patch("utils.venv_ops.ensure_venv") as venv:
            namespace = runpy.run_path(str(ROOT / "agent" / "main.py"), run_name="bootstrap_test")
        venv.assert_not_called()
        self.assertEqual(namespace["current_mode"], "android")
        self.assertEqual(os.environ["MAAFW_BINARY_PATH"], str(native))

    def test_mfaa_native_alias(self):
        native = self.libraries()
        os.environ["MAA_LIBRARY_DIR"] = str(native)
        self.assertEqual(runtime.android_library_dir(), native)
        self.assertEqual(os.environ["MAAFW_BINARY_PATH"], str(native))

    def test_missing_agent_library_fails_before_registration(self):
        native = self.libraries()
        (native / "libMaaAgentServer.so").unlink()
        os.environ["MAAFW_BINARY_PATH"] = str(native)
        with self.assertRaisesRegex(RuntimeError, "libMaaAgentServer"):
            runtime.android_library_dir()

    def test_missing_native_path_does_not_guess_desktop_runtimes(self):
        with self.assertRaisesRegex(RuntimeError, "host must provide"):
            runtime.android_library_dir()

    def test_maafwapp_data_is_outside_replaceable_pi(self):
        os.environ["PI_CLIENT_NAME"] = "MaaFwApp"
        self.assertEqual(runtime.persistent_data_dir(self.root / "pi"), self.root / "mfabd2-save")
        with self.assertRaisesRegex(RuntimeError, "Unknown Android host"):
            runtime.persistent_data_dir(self.root / "unexpected-layout")

    def test_mfaa_data_uses_preserved_config(self):
        os.environ["MFA_ANDROID_OUTPUT_BRIDGED"] = "1"
        self.assertEqual(runtime.persistent_data_dir(self.root), self.root / "config" / "MFABD2")

    def test_desktop_still_uses_legacy_path_selection(self):
        self.assertIsNone(runtime.persistent_data_dir(self.root))

    def test_relative_override_is_rejected(self):
        os.environ["MFABD2_DATA_DIR"] = "relative/save"
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            self.store.load()
        self.assertFalse(self.store._initialized)

    def test_unwritable_host_path_never_falls_back_or_changes_existing_save(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("keep", encoding="utf-8")
        os.environ["MFABD2_DATA_DIR"] = str(blocked)
        with self.assertRaises(OSError):
            self.store.set("key", "new")
        self.assertEqual(blocked.read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.store._initialized)

    def test_accounts_backup_restore_and_resource_replacement(self):
        import shutil
        resource = self.root / "pi"
        resource.mkdir()
        data = self.root / "mfabd2-save"
        os.environ["MFABD2_DATA_DIR"] = str(data)
        self.store.set("value", "zero")
        self.store.switch_account("two")
        self.store.set("value", "second")
        shutil.rmtree(resource)  # Simulate host replacing only its resource subtree.
        resource.mkdir()
        self.store.FILE_PATH.unlink()  # Recover the active account from its .bak.
        self.assertEqual(self.store.get("value"), "second")
        self.store.switch_account("0")
        self.assertEqual(self.store.get("value"), "zero")
        self.assertEqual(self.store.FILE_PATH.parent, data)

    def test_unreadable_existing_save_remains_write_protected(self):
        os.environ["MFABD2_DATA_DIR"] = str(self.root / "save")
        self.store.set("existing", "keep")
        previous = self.store.FILE_PATH.read_bytes()
        with patch.object(self.store, "_try_load_file", return_value=(None, "unreadable")):
            self.store.set("new", "must-not-overwrite")
        self.assertEqual(self.store.FILE_PATH.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
