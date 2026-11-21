"""Portable source/resource wiring checks for the PC stealing tasks."""

import ast
import json
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steal_test_helpers import ROOT, BASE, PC, load_action


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.nodes = {}
        self.private = {}
        for layer in (BASE, PC):
            for path in sorted((layer / "pipeline").rglob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                for name, node in data.items():
                    self.nodes.setdefault(name, {}).update(node)
                if path.name.startswith("Steal"):
                    self.assertFalse(set(self.private).intersection(data))
                    self.private.update(data)

    def test_private_nodes_have_explicit_failure_edges_and_no_public_overrides(self):
        self.assertGreater(len(self.private), 200)
        for name, node in self.private.items():
            with self.subTest(name=name):
                self.assertTrue(name.startswith("Steal"))
                self.assertIn("on_error", node)
                self.assertNotIn("Global_Null", node.get("on_error", []))
        for name in ("Steal_RouteFail", "StealInteraction_Fail"):
            self.assertEqual(self.private[name]["custom_action"], "StealFail")
            self.assertEqual(self.private[name]["next"], [])
            self.assertEqual(self.private[name]["on_error"], [])

    def test_static_node_references_resolve(self):
        for name, node in self.private.items():
            for item in objects(node):
                for field in ("next", "on_error", "all_of", "any_of"):
                    for ref in item.get(field, []):
                        if isinstance(ref, str):
                            while ref.startswith("[JumpBack]"):
                                ref = ref[len("[JumpBack]"):]
                            self.assertIn(ref, self.nodes, (name, field, ref))
                if item.get("custom_recognition") == "NotRecognition":
                    self.assertIn(item["custom_recognition_param"]["node"], self.nodes)

    def test_static_and_dynamic_images_exist_and_are_valid_pngs(self):
        templates = set()
        for node in self.private.values():
            for item in objects(node):
                raw = item.get("template", [])
                templates.update([raw] if isinstance(raw, str) else raw)
        module = load_action("steal_navigation")
        for card in range(1, 9):
            templates.update(module.navigation_overrides({"card": card, "destination": "shop",
                "expected_npc_names": ["目标商店老板"]})["StealNav_Card"]["template"])
        for template in sorted(templates):
            with self.subTest(template=template):
                path = PC / "image" / template
                if not path.is_file():
                    path = BASE / "image" / template
                self.assertTrue(path.is_file(), template)
                data = path.read_bytes()
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertTrue(all(value > 0 for value in struct.unpack(">II", data[16:24])))

    def test_callbacks_are_declared_in_source_and_imported_for_registration(self):
        registered = {"custom_action": set(), "custom_recognition": set()}
        for path in (ROOT / "agent").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for deco in node.decorator_list:
                    if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                            and deco.func.attr in registered and deco.args
                            and isinstance(deco.args[0], ast.Constant)):
                        registered[deco.func.attr].add(deco.args[0].value)
        for node in self.private.values():
            for item in objects(node):
                for field in registered:
                    if field in item:
                        self.assertIn(item[field], registered[field])
        imports = (ROOT / "agent/action/__init__.py").read_text(encoding="utf-8")
        for name in ("steal_flow", "steal_interaction", "steal_navigation", "steal_weekly"):
            self.assertIn(f"from .{name} import *", imports)


if __name__ == "__main__":
    unittest.main(verbosity=2)
