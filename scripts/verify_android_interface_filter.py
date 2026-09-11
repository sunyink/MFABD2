import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


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
        self.assertEqual(filtered["task"], self.interface["task"])

    def test_task_and_preset_references_follow_controller_filter(self):
        source = deepcopy(self.interface)
        source["task"].append({"name": "pc-only", "controller": ["PC客户端"]})
        source["preset"][0]["task"].append({"name": "pc-only"})
        filtered = INSTALL.prepare_interface_for_target(source, "android")
        self.assertNotIn("pc-only", [task["name"] for task in filtered["task"]])
        self.assertNotIn("pc-only", [task["name"] for task in filtered["preset"][0]["task"]])

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
