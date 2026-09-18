"""Focused checks for optional icon preparation and rollback boundaries."""

import io
import json
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

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

    def test_failed_interface_write_removes_new_or_restores_existing_image(self):
        title = self.install / "resource/ui/title.png"
        title.parent.mkdir(parents=True)
        write = icons.atomic_write

        def fail_interface(path, data):
            if path == self.interface:
                raise OSError("interface write failed")
            write(path, data)

        for previous in (None, b"previous image"):
            with self.subTest(previous=previous):
                if previous is not None:
                    title.write_bytes(previous)
                with patch.object(icons, "atomic_write", side_effect=fail_interface):
                    with self.assertRaisesRegex(OSError, "interface write failed"):
                        icons.apply_runtime(self.install, self.branding)
                self.assertEqual(self.interface.read_bytes(), self.original)
                self.assertEqual(title.read_bytes() if title.exists() else None, previous)

    @staticmethod
    def png_chunks(*chunks):
        return b"\x89PNG\r\n\x1a\n" + b"".join(
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
            for kind, body in chunks
        )

    def test_malformed_png_errors_are_controlled(self):
        valid = (self.branding / "title.png").read_bytes()
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        stream = zlib.compress(b"\0" * 5)
        cases = {
            "short chunk header": valid[:10],
            "short chunk body": valid[:24],
            "forged length": valid[:8] + struct.pack(">I", 0xffffffff) + valid[12:],
            "wrong IHDR size": self.png_chunks((b"IHDR", b"bad"), (b"IEND", b"")),
            "invalid deflate": self.png_chunks((b"IHDR", header), (b"IDAT", b"bad"), (b"IEND", b"")),
            "unfinished deflate": self.png_chunks((b"IHDR", header), (b"IDAT", stream[:-1]), (b"IEND", b"")),
            "extra deflate data": self.png_chunks((b"IHDR", header), (b"IDAT", stream + b"extra"), (b"IEND", b"")),
            "oversized pixels": self.png_chunks((b"IHDR", header), (b"IDAT", zlib.compress(b"\0" * 100000)), (b"IEND", b"")),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                path = self.root / "malformed.png"
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    icons.validate_png(path)

    def test_oversized_png_file_is_rejected_before_parsing(self):
        path = self.root / "oversized.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * icons.MAX_PNG_BYTES)
        with self.assertRaisesRegex(ValueError, "exceeds 8 MiB"):
            icons.validate_png(path)

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

    def start(self, patcher):
        """addCleanup, not enterContext: the same source runs on the 3.10 build jobs."""
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def mock_tool(self):
        self.start(patch.object(icons, "RCEDIT_SHA256", icons.hashlib.sha256(b"test tool").hexdigest()))
        self.start(patch.object(icons.urllib.request, "urlopen", return_value=io.BytesIO(b"test tool")))

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

    def test_malformed_ico_errors_are_controlled(self):
        valid = (self.branding / "app.ico").read_bytes()
        entry = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, 16, 6 + 16 * 7)
        cases = {
            "empty file": b"",
            "short header": valid[:5],
            "truncated directory": valid[:6] + valid[6:20],
            "frame beyond file": valid[:6] + entry * 7,
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                path = self.root / "malformed.ico"
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    icons.ico_frames(path)

    def test_oversized_ico_file_is_rejected_before_parsing(self):
        path = self.root / "oversized.ico"
        path.write_bytes(b"\0" * (icons.MAX_ICO_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "exceeds 8 MiB"):
            icons.ico_frames(path)

    def test_failed_profile_write_does_not_enable_retry(self):
        original = self.profile.read_bytes()
        write = icons.atomic_write

        def fail_profile(path, data):
            if path == self.profile:
                raise OSError("profile write failed")
            write(path, data)

        with patch.object(icons, "atomic_write", side_effect=fail_profile):
            with self.assertRaisesRegex(OSError, "profile write failed"):
                icons.apply_android(self.profile, self.work, self.branding)
        self.assertEqual(self.profile.read_bytes(), original)
        # The backup is written before the profile on purpose, so it must not be the
        # signal that burns a full --rerun-tasks rebuild.
        self.assertTrue((self.work / "branding/profile-before-icons.yaml").is_file())
        self.assertFalse(icons.restore_android(self.profile, self.work))

    def test_check_rejects_a_runtime_icon_that_did_not_land(self):
        icons.apply_runtime(self.install, self.branding)
        icons.check_runtime(self.install, self.branding)
        title = self.install / icons.RUNTIME_ICON
        title.write_bytes(b"not the branding image")
        with self.assertRaisesRegex(ValueError, "not the branding image"):
            icons.check_runtime(self.install, self.branding)
        title.unlink()
        with self.assertRaises(ValueError):
            icons.check_runtime(self.install, self.branding)
        self.interface.write_bytes(self.original)
        with self.assertRaisesRegex(ValueError, "interface.json icon"):
            icons.check_runtime(self.install, self.branding)

    def test_check_rejects_an_android_icon_that_did_not_land(self):
        icons.apply_android(self.profile, self.work, self.branding)
        icons.check_android(self.profile, self.work, self.branding)
        (self.work / "branding/launcher.png").write_bytes(b"not the branding image")
        with self.assertRaisesRegex(ValueError, "not the branding image"):
            icons.check_android(self.profile, self.work, self.branding)
        icons.android_marker(self.work).unlink()
        with self.assertRaisesRegex(ValueError, "left no marker"):
            icons.check_android(self.profile, self.work, self.branding)

    def test_status_output_distinguishes_applied_from_degraded(self):
        output = self.root / "step-output.txt"
        with patch.dict(icons.os.environ, {"GITHUB_OUTPUT": str(output)}):
            icons.publish_status(icons.optional("ok", lambda: None))
            icons.publish_status(icons.optional("bad", lambda: icons.validate_png(self.root / "absent")))
        self.assertEqual(output.read_text().split(), ["status=applied", "status=degraded"])

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
