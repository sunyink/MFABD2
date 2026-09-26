"""Instance selection/storage regression tests. All files live in temporary dirs."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from utils.instance_account_config import (
    ACCOUNT_INPUT, ACCOUNT_OPTION, STOP_ENTRY, SWITCH_OPTION, TASK_ENTRY,
    parse_instance_account, read_instance_account,
)
from utils.account_sync import AccountSession
from utils.persistent_store import AccountNotReadyError, PersistentStore
from utils.runtime_environment import StoragePolicy


def config(number="1", index=0, checked=True):
    return {"TaskItems": [{"name": "renamed by user", "entry": TASK_ENTRY, "default_check": checked,
                           "option": [{"name": SWITCH_OPTION, "index": index, "sub_options": [
                               {"name": ACCOUNT_OPTION, "data": {ACCOUNT_INPUT: number}}]}]}]}


def store_type():
    return type("TestStore", (PersistentStore,), {
        "_initialized": False, "_storage_policy": None, "_directory_initialized": False,
        "_account_ready": False, "_bound_task_key": None, "_mounted_account_id": None,
        "_current_account_id": None, "_sanitized_account_id": None, "_degraded_readonly": False,
        "CONFIG_DIR": None, "FILE_PATH": None, "BACKUP_PATH": None,
    })


class Context:
    def __init__(self, job):
        self.job = job
        self.stops = []

    def get_task_job(self):
        return SimpleNamespace(job_id=self.job)

    def run_action(self, name):
        self.stops.append(name)
        return SimpleNamespace(success=True)


class ConfigTests(unittest.TestCase):
    def test_upstream_serializer_fixtures(self):
        # These option trees were emitted by MFAA's unmodified converter.
        folder = ROOT / "tools/fixtures/account_config"
        for name, expected in (("yes-unchecked", "001"), ("no-checked", "0"), ("yes-halfchecked", "2")):
            data = json.loads((folder / f"mfaa-{name}.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(parse_instance_account(data).account_id, expected)

    def test_task_selection_and_name_do_not_select_account(self):
        for state in (True, False, None, "invalid-but-not-an-account-field"):
            self.assertEqual(parse_instance_account(config("001", checked=state)).account_id, "001")
        document = config("9")
        del document["TaskItems"][0]["default_check"]
        self.assertEqual(parse_instance_account(document).account_id, "9")

    def test_switch_no_ignores_hidden_input(self):
        for value in (None, "", "garbage", "123"):
            result = parse_instance_account(config(value, index=1))
            self.assertEqual((result.account_id, result.enabled), ("0", False))
        document = config(index=1)
        del document["TaskItems"][0]["option"][0]["sub_options"]
        self.assertEqual(parse_instance_account(document).account_id, "0")

    def test_yes_requires_ascii_digits_and_keeps_leading_zeroes(self):
        for value in ("0", "1", "001", "12345678901234567890"):
            self.assertEqual(parse_instance_account(config(value)).account_id, value)
        for value in ("", None, 1, True, " 1", "1 ", "-1", "1.0", "１２", "\0null"):
            with self.subTest(value=value):
                self.assertFalse(parse_instance_account(config(value)).valid)

    def test_invalid_or_missing_case_never_means_no(self):
        for index in (None, True, False, "1", -1, 2):
            self.assertFalse(parse_instance_account(config(index=index)).valid)

    def test_missing_task_and_invalid_structure(self):
        for document in (None, {}, {"TaskItems": []}, {"TaskItems": "bad"}):
            self.assertFalse(parse_instance_account(document).valid)
        document = config()
        document["TaskItems"][0]["entry"] = "Env_AccountSave_Switch"
        self.assertFalse(parse_instance_account(document).valid)

    def test_duplicate_task_and_option_conflicts(self):
        document = config("2")
        document["TaskItems"] *= 2
        self.assertEqual(parse_instance_account(document).account_id, "2")
        document = config("2")
        document["TaskItems"] += config("3")["TaskItems"]
        self.assertFalse(parse_instance_account(document).valid)
        document = config("0", index=1)
        document["TaskItems"] += config("0", index=0)["TaskItems"]
        self.assertFalse(parse_instance_account(document).valid)
        document = config("1")
        document["TaskItems"][0]["option"] += config("2")["TaskItems"][0]["option"]
        self.assertFalse(parse_instance_account(document).valid)
        document = config("1")
        children = document["TaskItems"][0]["option"][0]["sub_options"]
        children.append({"name": ACCOUNT_OPTION, "data": {ACCOUNT_INPUT: "2"}})
        self.assertFalse(parse_instance_account(document).valid)


class FileAndSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "config/instances"
        self.folder.mkdir(parents=True)
        self.store = store_type()
        self.store.configure_storage(StoragePolicy(self.root / "save"))
        self.session = AccountSession(self.root, "A", store=self.store)
        self.patches = [patch("utils.account_sync.logger.info"), patch("utils.account_sync.logger.error"),
                        patch("utils.persistent_store.logger.info"), patch("utils.persistent_store.logger.warning")]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def write(self, number="1", *, index=0, instance="A", checked=True):
        (self.folder / f"{instance}.json").write_text(
            json.dumps(config(number, index, checked)), encoding="utf-8-sig")

    def test_default_instance_and_path_safety(self):
        self.write("3", instance="default")
        self.assertEqual(read_instance_account(self.root, "default").account_id, "3")
        for identity in ("", "..", "../A", "..\\A", "A/B", "A:C"):
            self.assertFalse(read_instance_account(self.root, identity, attempts=1).valid)

    def test_truncated_file_retries_without_writing_ui_config(self):
        self.write("7")
        original = (self.folder / "A.json").read_bytes()
        with patch.object(Path, "read_text", side_effect=["{", json.dumps(config("7"))]), \
                patch("utils.instance_account_config.time.sleep") as sleep:
            self.assertEqual(read_instance_account(self.root, "A").account_id, "7")
            sleep.assert_called_once_with(0.1)
        self.assertEqual((self.folder / "A.json").read_bytes(), original)

    def test_retry_exhaustion_is_not_default_account(self):
        self.write()
        with patch.object(Path, "read_text", return_value="{"), patch("utils.instance_account_config.time.sleep") as sleep:
            self.assertFalse(read_instance_account(self.root, "A").valid)
            self.assertEqual(sleep.call_count, 3)

    def test_root_snapshot_then_next_task_and_rebinding(self):
        self.write("1", checked=False)
        first = Context(10)
        self.assertTrue(self.session.sync(first))
        self.assertTrue(self.store.set("progress", "one"))
        self.write("2")
        self.assertTrue(self.session.sync(first))
        self.assertEqual(self.store._current_account_id, "1")
        self.store.switch_account("9")
        self.assertTrue(self.session.sync(first))
        self.assertEqual(self.store._current_account_id, "1")
        self.assertTrue(self.session.sync(Context(11)))
        self.assertEqual(self.store._current_account_id, "2")
        self.assertTrue(self.store.set("progress", "two"))
        self.assertEqual(json.loads((self.root / "save/agent_save_data_1.json").read_text())["progress"], "one")

    def test_no_and_yes_zero_both_explicitly_select_default(self):
        self.write("broken", index=1)
        self.assertTrue(self.session.sync(Context(1)))
        self.assertEqual(self.store._current_account_id, "0")
        self.write("0", index=0)
        self.assertTrue(self.session.sync(Context(2)))
        self.assertEqual(self.store._current_account_id, "0")

    def test_failure_revokes_old_account_and_pins_failure(self):
        self.write("1")
        self.assertTrue(self.session.sync(Context(1)))
        self.store.set("keep", 42)
        originals = {p.name: p.read_bytes() for p in (self.root / "save").iterdir()}
        self.write("")
        failed = Context(2)
        self.assertFalse(self.session.sync(failed))
        self.assertEqual(failed.stops, [STOP_ENTRY])
        with self.assertRaises(AccountNotReadyError): self.store.load()
        self.assertFalse(self.store.save({"overwrite": True}))
        self.assertFalse(self.store.set("overwrite", True))
        self.write("2")
        self.assertFalse(self.session.sync(failed))
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "save").iterdir()}, originals)
        self.assertTrue(self.session.sync(Context(3)))
        self.assertEqual(self.store._current_account_id, "2")

    def test_preparation_and_mounting_have_no_account_file_side_effects(self):
        self.store.prepare_directory()
        with self.assertRaises(AccountNotReadyError): self.store.get("x")
        self.assertFalse(self.store.save({}))
        self.assertEqual(list((self.root / "save").iterdir()), [])
        self.store.bind_account("0", ("A", 1))
        self.assertEqual(list((self.root / "save").iterdir()), [])

    def test_blocked_load_does_not_restore_backup(self):
        self.store.prepare_directory()
        backup = self.root / "save/agent_save_data_1.json.bak"
        backup.write_text('{"keep":1}')
        self.store.bind_account("1", ("A", 1))
        self.store.block_account(("A", 2))
        with self.assertRaises(AccountNotReadyError): self.store.load()
        self.assertFalse((self.root / "save/agent_save_data_1.json").exists())
        self.assertEqual(backup.read_text(), '{"keep":1}')

    def test_rebind_preserves_readonly_condition(self):
        self.write("1")
        self.session.sync(Context(1))
        self.store.set("keep", 42)
        self.store._degraded_readonly = True
        self.store.block_account()
        self.assertTrue(self.session.sync(Context(1)))
        self.assertTrue(self.store._degraded_readonly)
        self.assertFalse(self.store.save({"bad": 0}))

    def test_two_instances_own_separate_decisions(self):
        self.write("1", instance="A")
        self.write("2", instance="B")
        other = store_type()
        other.configure_storage(StoragePolicy(self.root / "save"))
        b = AccountSession(self.root, "B", store=other)
        self.assertTrue(self.session.sync(Context(1)))
        self.assertTrue(b.sync(Context(1)))
        self.assertEqual((self.store._current_account_id, other._current_account_id), ("1", "2"))

    def test_missing_identity_is_not_android(self):
        self.assertFalse(AccountSession(self.root, "", store=self.store).sync(Context(1)))
        with patch("utils.account_sync.read_instance_account") as read:
            self.assertTrue(AccountSession(self.root, "", android=True, store=self.store).sync(Context(2)))
            read.assert_not_called()
        self.assertEqual(self.store._current_account_id, "0")

    def test_invalid_root_and_stop_failure_keep_storage_locked(self):
        self.write("1")
        context = Context(0)
        context.run_action = lambda _: None
        self.assertFalse(self.session.sync(context))
        self.assertFalse(self.store.save({}))


class InterfaceTests(unittest.TestCase):
    def test_ui_shape_presets_and_android_filter(self):
        interface = json.loads((ROOT / "assets/interface.json").read_text(encoding="utf-8"), strict=False)
        task = next(t for t in interface["task"] if t["entry"] == TASK_ENTRY)
        self.assertEqual((task["name"], task["label"], task["option"]),
                         ("多存档", "⚙️ 多存档", [SWITCH_OPTION]))
        switch = interface["option"][SWITCH_OPTION]
        self.assertEqual([c["name"] for c in switch["cases"]], ["Yes", "No"])
        self.assertEqual(switch["default_case"], "Yes")
        self.assertEqual(switch["cases"][0]["option"], [ACCOUNT_OPTION])
        initial = json.loads((ROOT / "tools/fixtures/account_config/mfaa-initial-option.json").read_text())
        # MFAA pre-populates Index=0 when reading the PI string option list;
        # default_case cannot replace an already populated index.
        self.assertEqual(switch["cases"][initial[0]["index"]]["name"], "Yes")
        option = interface["option"][ACCOUNT_OPTION]
        self.assertNotIn("pipeline_override", option)
        self.assertEqual((option["inputs"][0]["default"], option["inputs"][0]["verify"]), ("0", "^[0-9]+$"))
        self.assertNotIn(ACCOUNT_OPTION, interface["global_option"])
        for preset in interface["preset"]:
            self.assertEqual(next(t for t in preset["task"] if t["name"] == "多存档"), {"name": "多存档", "enabled": True})
        nodes = json.loads((ROOT / "assets/resource/base/pipeline/Dummy.json").read_text(encoding="utf-8"))
        self.assertFalse(set(nodes[TASK_ENTRY]) & {"action", "custom_action", "next"})
        self.assertEqual(nodes[STOP_ENTRY]["action"], "StopTask")
        spec = importlib.util.spec_from_file_location("test_install", ROOT / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        before = deepcopy(interface)
        apk = installer.prepare_interface_for_target(interface, "android-arm64")
        self.assertEqual(interface, before)
        self.assertFalse(any(t["entry"] == TASK_ENTRY for t in apk["task"]))
        self.assertNotIn(SWITCH_OPTION, apk["option"])
        self.assertNotIn(ACCOUNT_OPTION, apk["option"])
        self.assertTrue(all(t["name"] != "多存档" for p in apk["preset"] for t in p["task"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
