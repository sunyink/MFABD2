"""Focused checks for optional icon preparation and rollback boundaries."""

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prepare_icons as icons


class IconTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.install.mkdir()
        self.interface = self.install / "interface.json"
        self.original = (b'{"name":"fixture","icon":"old.png","task":[],"resource":'
                         b'[{"name":"base","path":["./resource/base"]}]}')
        self.interface.write_bytes(self.original)
        self.branding = icons.ROOT / "release" / "branding"
        self.executable = self.install / "MFAAvalonia.exe"
        self.executable.write_bytes(b"original executable")
        self.profile = self.root / "android/pi-profile.yaml"
        self.profile.parent.mkdir()
        self.profile.write_text('app:\n  id: mfabd2.ci\n  label: MFABD2-CI\n  icon: ../ReadMe/logo.png\n')
        self.work = self.root / "android-build"

    def test_runtime_copies_only_ui_png_and_preserves_task_resources(self):
        existing_resource = self.install / "resource/base/existing.txt"
        existing_resource.parent.mkdir(parents=True)
        existing_resource.write_bytes(b"existing task resource")
        icons.apply_runtime(self.install, self.branding)
        interface = json.loads(self.interface.read_text())
        self.assertEqual(interface["icon"], "resource/ui/title.png")
        self.assertEqual(interface["task"], [])
        self.assertEqual(interface["resource"], json.loads(self.original)["resource"])
        self.assertEqual(existing_resource.read_bytes(), b"existing task resource")
        self.assertEqual((self.install / interface["icon"]).read_bytes(), (self.branding / "title.png").read_bytes())
        self.assertEqual(
            {path.relative_to(self.install).as_posix() for path in self.install.rglob("*") if path.is_file()},
            {"interface.json", "MFAAvalonia.exe", "resource/base/existing.txt", "resource/ui/title.png"},
        )

    def test_missing_runtime_art_leaves_interface_untouched(self):
        with self.assertRaises(FileNotFoundError):
            icons.apply_runtime(self.install, self.root / "absent")
        self.assertEqual(self.interface.read_bytes(), self.original)

    def test_failed_image_copy_never_changes_interface(self):
        with patch.object(icons, "atomic_write", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                icons.apply_runtime(self.install, self.branding)
        self.assertEqual(self.interface.read_bytes(), self.original)

    def test_corrupt_png_is_rejected(self):
        source = bytearray((self.branding / "android.png").read_bytes())
        source[50] ^= 255
        broken = self.root / "broken.png"
        broken.write_bytes(source)
        with self.assertRaises(ValueError):
            icons.validate_png(broken)

    def test_approved_inputs(self):
        for name in ("title.png", "android.png", "frame.png"):
            icons.validate_png(self.branding / name)
        self.assertEqual(len(icons.ico_frames(self.branding / "app.ico")), 7)
        self.assertEqual((self.branding / "app.icns").read_bytes()[:4], b"icns")

    def test_tool_checksum_failure_leaves_executable_untouched(self):
        with patch.object(icons.urllib.request, "urlopen", return_value=io.BytesIO(b"bad download")):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                icons.apply_windows(self.install, self.branding)
        self.assertEqual(self.executable.read_bytes(), b"original executable")

    def mock_tool(self):
        self.enterContext(patch.object(icons, "RCEDIT_SHA256", icons.hashlib.sha256(b"test tool").hexdigest()))
        self.enterContext(patch.object(icons.urllib.request, "urlopen", return_value=io.BytesIO(b"test tool")))

    def test_tool_partial_write_failure_leaves_executable_untouched(self):
        self.mock_tool()

        def fail(command, **kwargs):
            Path(command[1]).write_bytes(b"damaged")
            raise subprocess.CalledProcessError(1, command)

        with patch.object(icons.subprocess, "run", side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                icons.apply_windows(self.install, self.branding)
        self.assertEqual(self.executable.read_bytes(), b"original executable")

    def test_tool_success_without_icon_payloads_is_rejected(self):
        self.mock_tool()
        with patch.object(icons.subprocess, "run"):
            with self.assertRaisesRegex(ValueError, "missing an approved"):
                icons.apply_windows(self.install, self.branding)
        self.assertEqual(self.executable.read_bytes(), b"original executable")

    def test_android_copy_and_rollback_preserve_identity(self):
        original = self.profile.read_bytes()
        icons.apply_android(self.profile, self.work, self.branding)
        current = self.profile.read_text()
        self.assertIn("id: mfabd2.ci", current)
        self.assertIn("label: MFABD2-CI", current)
        icon = json.loads(icons.PROFILE_ICON.search(current)[0].split(": ", 1)[1])
        self.assertEqual((self.profile.parent / icon).read_bytes(), (self.branding / "android.png").read_bytes())
        self.assertTrue(icons.restore_android(self.profile, self.work))
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertFalse(icons.restore_android(self.profile, self.work))

    def test_missing_android_art_does_not_create_retry_backup(self):
        original = self.profile.read_bytes()
        with self.assertRaises(FileNotFoundError):
            icons.apply_android(self.profile, self.work, self.root / "absent")
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertFalse(icons.restore_android(self.profile, self.work))

    def test_second_android_prepare_does_not_overwrite_original_backup(self):
        original = self.profile.read_bytes()
        icons.apply_android(self.profile, self.work, self.branding)
        with self.assertRaises(ValueError):
            icons.apply_android(self.profile, self.work, self.branding)
        icons.restore_android(self.profile, self.work)
        self.assertEqual(self.profile.read_bytes(), original)

    def test_failure_reports_warning_and_allows_next_operation(self):
        summary = self.root / "summary.md"
        with patch.dict(icons.os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                icons.optional("first", lambda: icons.validate_png(self.root / "absent"))
                icons.optional("next", lambda: icons.apply_runtime(self.install, self.branding))
        self.assertIn("::warning", output.getvalue())
        self.assertIn("kept default", summary.read_text())
        self.assertIn("next: applied", summary.read_text())


if __name__ == "__main__":
    unittest.main()
