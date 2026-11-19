"""install.py packaging per target: which files go where. interface.json rules are tested in
verify_release_interface.py; this file covers the copying around it.
"""

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("mfabd2_install", ROOT / "install.py")
INSTALL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALL)


class DistributionDocumentTest(unittest.TestCase):
    def test_packaging_copies_statement_for_all_targets(self):
        for target in ("win", "linux", "macos", "android"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as work:
                output = Path(work)
                with patch.multiple(INSTALL, install_path=output, target_os=target):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        INSTALL.install_chores()
                self.assertEqual((output / "TRADEMARKS.md").read_bytes(),
                                 (ROOT / "TRADEMARKS.md").read_bytes())

    def test_missing_statement_stops_packaging(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            output = root / "install"
            output.mkdir()
            for name in ("README.md", "LICENSE", "LICENSE-APACHE", "LICENSE-MIT"):
                (root / name).write_text("fixture", encoding="utf-8")
            with patch.multiple(INSTALL, working_dir=root, install_path=output):
                with self.assertRaises(FileNotFoundError):
                    INSTALL.install_chores()


class ResourcePackTest(unittest.TestCase):
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
            (assets / "mfa_layout.json").write_text("{}", encoding="utf-8")
            source_files = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            with patch.multiple(INSTALL, working_dir=root, install_path=output, version="v0.0.1"), \
                    patch.object(INSTALL, "configure_ocr_model"), patch("sys.stdout", new_callable=io.StringIO):
                # Reuse one output directory to exercise desktop -> Android cleanup.
                for target in ("win-x64", "android-arm64", "android", "linux-x64"):
                    with self.subTest(target=target), patch.object(INSTALL, "target_os", target):
                        INSTALL.install_resource()
                        expected = {"base", "android_native", "Announcement"} if target.startswith("android") else packs
                        self.assertEqual({path.name for path in (output / "resource").iterdir() if path.is_dir()}, expected)
                        for name in expected:
                            self.assertEqual((output / "resource" / name / "nested" / "content.txt").read_text(encoding="utf-8"), name)
                        self.assertEqual((output / "resource" / "mfa_layout.json").read_text(), "{}")
                        # interface.json is written later, once, by install_interface.
                        self.assertFalse((output / "interface.json").exists())
            self.assertEqual({path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}, source_files)

    def test_unknown_target_stops_before_copying(self):
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "install"
            with patch.multiple(INSTALL, install_path=output, target_os="mac"), \
                    patch.object(INSTALL, "configure_ocr_model"), self.assertRaises(ValueError):
                INSTALL.install_resource()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
