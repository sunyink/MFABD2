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
        self.store = type("IsolatedStore", (PersistentStore,), {"_initialized": False, "_storage_policy": None})
        for method in ("info", "warning", "error"):
            self.enterContext(patch(f"utils.persistent_store.logger.{method}"))

    def android_config(self):
        if "MFA_ANDROID_OUTPUT_BRIDGED" not in os.environ:
            os.environ["PI_CLIENT_NAME"] = "MaaFwApp"
        return runtime.RuntimeConfig.detect(self.root / "pi")

    def libraries(self):
        path = self.root / "native"
        path.mkdir()
        for name in ("libMaaFramework.so", "libMaaAgentServer.so"):
            (path / name).touch()
        return path

    def test_host_marks_android_even_when_python_reports_linux(self):
        os.environ["PI_CLIENT_NAME"] = "MaaFwApp"
        os.environ["MAAFW_BINARY_PATH"] = str(self.libraries())
        self.assertEqual(self.android_config().mode, "android")

    def test_native_path_is_preserved(self):
        native = self.libraries()
        os.environ["MAAFW_BINARY_PATH"] = str(native)
        config = self.android_config()
        config.prepare()
        self.assertEqual(config.library_dir, native)
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
        os.environ["MFABD2_DATA_DIR"] = str(self.root / "save")
        with patch.dict(sys.modules, modules), patch("utils.venv_ops.ensure_venv") as venv, patch.object(PersistentStore, "configure_storage") as configure:
            namespace = runpy.run_path(str(ROOT / "agent" / "main.py"), run_name="bootstrap_test")
        venv.assert_not_called()
        self.assertEqual(namespace["runtime"].mode, "android")
        configure.assert_called_once_with(namespace["runtime"].storage)
        self.assertEqual(os.environ["MAAFW_BINARY_PATH"], str(native))

    def test_mfaa_native_alias(self):
        native = self.libraries()
        os.environ["MAA_LIBRARY_DIR"] = str(native)
        os.environ["MFA_ANDROID_OUTPUT_BRIDGED"] = "1"
        config = self.android_config()
        config.prepare()
        self.assertEqual(config.library_dir, native)
        self.assertEqual(os.environ["MAAFW_BINARY_PATH"], str(native))

    def test_missing_agent_library_fails_before_registration(self):
        native = self.libraries()
        (native / "libMaaAgentServer.so").unlink()
        os.environ["MAAFW_BINARY_PATH"] = str(native)
        with self.assertRaisesRegex(RuntimeError, "libMaaAgentServer"):
            self.android_config().prepare()

    def test_missing_native_path_does_not_guess_desktop_runtimes(self):
        with self.assertRaisesRegex(RuntimeError, "host must provide"):
            self.android_config().prepare()

    def test_maafwapp_data_is_outside_replaceable_pi(self):
        os.environ["PI_CLIENT_NAME"] = "MaaFwApp"
        self.assertEqual(runtime.resolve_storage_policy(self.root / "pi"), runtime.StoragePolicy(self.root / "mfabd2-save"))
        with self.assertRaisesRegex(RuntimeError, "Unknown Android host"):
            runtime.resolve_storage_policy(self.root / "unexpected-layout")

    def test_mfaa_data_uses_preserved_config(self):
        os.environ["MFA_ANDROID_OUTPUT_BRIDGED"] = "1"
        self.assertEqual(runtime.resolve_storage_policy(self.root), runtime.StoragePolicy(self.root / "config" / "MFABD2"))

    def test_desktop_still_uses_legacy_path_selection(self):
        self.assertEqual(runtime.resolve_storage_policy(self.root).portable_root, self.root)

    def test_desktop_release_library_locations(self):
        for system, arch, rid in (
            ("Windows", "AMD64", "win-x64"),
            ("Windows", "ARM64", "win-arm64"),
            ("Linux", "x86_64", "linux-x64"),
            ("Linux", "aarch64", "linux-arm64"),
            ("Darwin", "x86_64", "osx-x64"),
            ("Darwin", "arm64", "osx-arm64"),
        ):
            with self.subTest(rid=rid), patch.object(runtime.platform, "system", return_value=system), patch.object(runtime.platform, "machine", return_value=arch):
                config = runtime.RuntimeConfig.detect(self.root)
                self.assertEqual(config.library_dir, self.root / "runtimes" / rid / "native")
                self.assertFalse(config.manage_venv)
                self.assertFalse(config.strict_storage)
                self.assertEqual(config.prepend_library_to_path, system == "Windows")

    def test_development_environment_policy(self):
        (self.root / "requirements.txt").touch()
        config = runtime.RuntimeConfig.detect(self.root)
        self.assertTrue(config.manage_venv)
        self.assertIsNone(config.library_dir)
        with patch("utils.venv_ops.ensure_venv") as venv:
            config.prepare()
        venv.assert_called_once_with(self.root)
        disabled = runtime.RuntimeConfig.detect(self.root, enable_venv_auto_check=False)
        self.assertFalse(disabled.manage_venv)
        with patch.object(runtime.platform, "system", return_value="Windows"), patch.object(sys, "executable", str(self.root / "python" / "python.exe")):
            embedded = runtime.RuntimeConfig.detect(self.root)
        self.assertFalse(embedded.manage_venv)

    def test_desktop_save_locations(self):
        os.environ["APPDATA"] = str(self.root / "roaming")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg")
        self.assertEqual(runtime.resolve_storage_policy(self.root, system="windows").directory, self.root / "roaming" / "MFABD2")
        self.assertEqual(runtime.resolve_storage_policy(self.root, system="linux").directory, self.root / "xdg" / "MFABD2")
        with patch.object(runtime.os.path, "expanduser", return_value=str(self.root / "application-support")):
            self.assertEqual(runtime.resolve_storage_policy(self.root, system="darwin").directory, self.root / "application-support" / "MFABD2")

    def test_desktop_portable_backup_takes_precedence(self):
        portable = self.root / "portable"
        portable.mkdir()
        (portable / "agent_save_data.json.bak").write_text('{"keep":42}', encoding="utf-8")
        self.store.configure_storage(runtime.StoragePolicy(self.root / "global", portable))
        self.assertEqual(self.store.get("keep"), 42)
        self.assertEqual(self.store.FILE_PATH.parent, portable)
        self.assertFalse((self.root / "global").exists())

    def test_only_desktop_policy_allows_portable_fallback(self):
        blocked = self.root / "blocked"
        blocked.touch()
        portable = self.root / "portable"
        portable.mkdir()
        self.store.configure_storage(runtime.StoragePolicy(blocked, portable))
        self.store.set("keep", 42)
        self.assertEqual(self.store.get("keep"), 42)
        self.assertEqual(self.store.FILE_PATH.parent, portable)

    def test_account_switch_does_not_redetect_environment(self):
        original = self.root / "first"
        os.environ["MFABD2_DATA_DIR"] = str(original)
        self.store.set("keep", 1)
        os.environ["MFABD2_DATA_DIR"] = str(self.root / "other")
        self.store.switch_account("second")
        self.store.set("keep", 2)
        self.assertEqual(self.store.FILE_PATH.parent, original)
        self.assertFalse((self.root / "other").exists())

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
