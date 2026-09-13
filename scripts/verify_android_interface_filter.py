import importlib.util
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("mfabd2_install", ROOT / "install.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class AndroidInterfaceFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "assets" / "interface.json").open(encoding="utf-8") as handle:
            cls.interface = json.load(handle, strict=False)

    def test_real_interface_keeps_only_adb_and_adb_resource(self):
        filtered = INSTALL.prepare_interface_for_target(self.interface, "android-arm64")
        self.assertEqual([item["name"] for item in filtered["controller"]], ["Adb"])
        self.assertEqual([item["name"] for item in filtered["resource"]], ["ADB"])
        self.assertEqual(filtered["resource"][0]["path"], ["./resource/base", "./resource/android_native"])
        self.assertNotIn("mirrorchyan_rid", filtered)
        expected_tasks = [task for task in self.interface["task"]
                          if not task.get("controller") or "Adb" in task["controller"]]
        self.assertEqual(filtered["task"], expected_tasks)

    def test_task_and_preset_references_follow_controller_filter(self):
        source = deepcopy(self.interface)
        source["task"].append({"name": "pc-only", "controller": ["PC客户端"]})
        source["preset"][0]["task"].append({"name": "pc-only"})
        filtered = INSTALL.prepare_interface_for_target(source, "android")
        self.assertNotIn("pc-only", [task["name"] for task in filtered["task"]])
        self.assertNotIn("pc-only", [task["name"] for task in filtered["preset"][0]["task"]])

    def test_pc_pretask_is_not_shipped_to_android(self):
        filtered = INSTALL.prepare_interface_for_target(self.interface, "android")
        self.assertEqual(filtered.get("pretask"), [])
        self.assertTrue(self.interface["pretask"])
        source = deepcopy(self.interface)
        source["pretask"].extend([
            {"name": "shared", "exec": "test"},
            {"name": "adb-only", "exec": "test", "controller": ["Adb"]},
        ])
        filtered = INSTALL.prepare_interface_for_target(source, "android")
        self.assertEqual([task["name"] for task in filtered["pretask"]], ["shared", "adb-only"])

    def test_windows_install_rewrites_pretask_paths(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "agent").mkdir()
            (root / "agent" / "pc_bootstrap.py").write_text("# fixture", encoding="utf-8")
            output = root / "install"
            output.mkdir()
            (output / "interface.json").write_text(json.dumps(self.interface), encoding="utf-8")
            with patch.multiple(INSTALL, working_dir=root, install_path=output):
                INSTALL.install_agent("win-x64")
            installed = json.loads((output / "interface.json").read_text(encoding="utf-8"))
            pretask = installed["pretask"][0]
            self.assertEqual(pretask["exec"], "../../python/python.exe")
            self.assertEqual(pretask["args"][-1], "../../agent/pc_bootstrap.py")
            base = output / "resource" / "base"
            self.assertEqual((base / pretask["args"][-1]).resolve(), (output / "agent" / "pc_bootstrap.py").resolve())
            self.assertTrue((output / "agent" / "pc_bootstrap.py").is_file())
        pretask = self.interface["pretask"][0]
        self.assertEqual((ROOT / "assets/resource/base" / pretask["args"][-1]).resolve(), ROOT / "agent/pc_bootstrap.py")
        self.assertEqual((ROOT / "assets/resource/base" / pretask["exec"]).resolve(), ROOT / ".venv/Scripts/python.exe")

    def install_agent_fixture(self, interface, target, tamper=None):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "agent").mkdir()
            (root / "agent" / "pc_bootstrap.py").write_text("# fixture", encoding="utf-8")
            output = root / "install"
            output.mkdir()
            interface_path = output / "interface.json"
            interface_path.write_text(json.dumps(interface), encoding="utf-8")
            original_dump = INSTALL.jsonc.dump

            def dump(value, handle, **kwargs):
                value = deepcopy(value)
                if tamper is not None:
                    tamper(value)
                return original_dump(value, handle, **kwargs)

            with patch.multiple(INSTALL, working_dir=root, install_path=output), \
                    patch.object(INSTALL.jsonc, "dump", side_effect=dump):
                INSTALL.install_agent(target)
            return json.loads(interface_path.read_text(encoding="utf-8"))

    def test_renamed_bootstrap_preserves_args_and_matches_both_slashes(self):
        for script in ("../../../agent/pc_bootstrap.py", r"..\..\..\agent\pc_bootstrap.py"):
            with self.subTest(script=script):
                source = {
                    "controller": [{"name": "renamed-pc", "type": "Win32"}],
                    "pretask": [{"name": "renamed-bootstrap", "controller": ["renamed-pc"],
                                 "exec": "../../../.venv/Scripts/python.exe",
                                 "args": ["-B", "-u", script, "--custom-flag", "value"]}],
                }
                installed = self.install_agent_fixture(source, "win-x64")
                self.assertEqual(installed["pretask"], [
                    {**source["pretask"][0], "exec": "../../python/python.exe",
                     "args": ["-B", "-u", "../../agent/pc_bootstrap.py", "--custom-flag", "value"]},
                ])

    def test_unrelated_pretask_venv_paths_fail_with_name(self):
        for field, value in (("exec", "../../../.venv/Scripts/python.exe"),
                             ("args", [r"..\.venv\other.py"])):
            with self.subTest(field=field):
                pretask = {"name": "unrelated-tool", "exec": "python3", "args": ["other.py"], field: value}
                with patch("sys.stdout", new_callable=io.StringIO) as output, \
                        self.assertRaises(SystemExit) as raised:
                    self.install_agent_fixture({"pretask": [pretask]}, "win-x64")
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("pretask 'unrelated-tool'", output.getvalue())
                self.assertIn(".venv", output.getvalue())

    def test_non_windows_drops_only_exclusively_win32_pretasks(self):
        source = {
            "controller": [{"name": "renamed-pc", "type": "Win32"},
                           {"name": "second-pc", "type": "Win32"},
                           {"name": "phone", "type": "Adb"},
                           {"name": "mac", "type": "PlayCover"}],
            "pretask": [
                {"name": "pc-only", "controller": ["renamed-pc"], "exec": ".venv/python"},
                {"name": "two-pcs", "controller": ["renamed-pc", "second-pc"], "exec": ".venv/python"},
                {"name": "shared", "exec": "python3", "args": ["shared.py"]},
                {"name": "empty", "controller": [], "exec": "python3"},
                {"name": "mixed-adb", "controller": ["renamed-pc", "phone"], "exec": "python3"},
                {"name": "mixed-other", "controller": ["renamed-pc", "mac"], "exec": "python3"},
                {"name": "other-only", "controller": ["mac"], "exec": "python3"},
            ],
        }
        for target in ("macos-arm64", "linux-x64"):
            with self.subTest(target=target):
                installed = self.install_agent_fixture(source, target)
                self.assertEqual(installed["pretask"], source["pretask"][2:])

    def test_pretask_readback_tampering_fails(self):
        source = {"pretask": [{"name": "shared", "exec": "python3", "args": ["shared.py"]}]}

        def tamper(value):
            value["pretask"][0]["args"] = ["different.py"]

        with patch("sys.stdout", new_callable=io.StringIO) as output, \
                self.assertRaises(SystemExit) as raised:
            self.install_agent_fixture(source, "linux-x64", tamper)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("pretask 回读不符", output.getvalue())

    def test_installed_resources_follow_target_and_remove_stale_packs(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            assets = root / "assets"
            source = assets / "resource"
            output = root / "install"
            packs = {"base", "android_native", "Announcement", "pc", "playcover", "future_platform"}
            for name in packs:
                directory = source / name / "nested"
                directory.mkdir(parents=True)
                (directory / "content.txt").write_text(name, encoding="utf-8")
            (assets / "interface.json").write_text(json.dumps(self.interface), encoding="utf-8")
            (assets / "mfa_layout.json").write_text("{}", encoding="utf-8")
            source_files = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            with patch.multiple(INSTALL, working_dir=root, install_path=output, version="v0.0.1"), \
                    patch.object(INSTALL, "configure_ocr_model"):
                # Reuse one output directory to exercise desktop -> Android cleanup.
                for target in ("win-x64", "android-arm64", "android", "linux-x64"):
                    with self.subTest(target=target), patch.object(INSTALL, "target_os", target):
                        INSTALL.install_resource()
                        expected = {"base", "android_native", "Announcement"} if target.startswith("android") else packs
                        self.assertEqual({path.name for path in (output / "resource").iterdir() if path.is_dir()}, expected)
                        for name in expected:
                            self.assertEqual((output / "resource" / name / "nested" / "content.txt").read_text(encoding="utf-8"), name)
                        self.assertEqual((output / "resource" / "mfa_layout.json").read_text(), "{}")
                        installed = json.loads((output / "interface.json").read_text(encoding="utf-8"))
                        self.assertEqual(installed["resource"], INSTALL.prepare_interface_for_target(self.interface, target)["resource"])
            self.assertEqual({path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}, source_files)

    def test_desktop_targets_are_unfiltered(self):
        filtered = INSTALL.prepare_interface_for_target(self.interface, "win-x64")
        self.assertEqual(filtered, self.interface)

    def test_unrestricted_resources_are_kept(self):
        source = {"controller": [{"name": "A", "type": "Adb"}], "resource": [{"name": "all", "path": ["."]}, {"name": "a", "path": ["."], "controller": ["A"]}, {"name": "b", "path": ["."], "controller": ["B"]}]}
        filtered = INSTALL.prepare_interface_for_target(source, "android")
        self.assertEqual([item["name"] for item in filtered["resource"]], ["all", "a"])

    def test_source_is_not_mutated(self):
        source = deepcopy(self.interface)
        INSTALL.prepare_interface_for_target(source, "android")
        self.assertEqual(source, self.interface)

    def test_missing_adb_fails(self):
        with self.assertRaises(ValueError):
            INSTALL.prepare_interface_for_target({"controller": [{"name": "PC", "type": "Win32"}], "resource": [{"name": "x"}]}, "android")

    def test_empty_result_fails(self):
        source = {"controller": [{"name": "A", "type": "Adb"}], "resource": [{"name": "b", "path": ["."], "controller": ["B"]}]}
        with self.assertRaises(ValueError):
            INSTALL.prepare_interface_for_target(source, "android")

    def test_resource_without_a_base_layer_fails(self):
        """An overlay-only resource loads fine and then has almost no nodes."""
        for resource in ({"name": "x"}, {"name": "x", "path": []}):
            source = {"controller": [{"name": "A", "type": "Adb"}], "resource": [resource]}
            with self.assertRaises(ValueError):
                INSTALL.prepare_interface_for_target(source, "android")


if __name__ == "__main__":
    unittest.main()
