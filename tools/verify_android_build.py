"""Small regression checks for the Android release metadata helper."""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = Path(os.environ.get("MFABD2_ANDROID_UPSTREAM", ROOT / "android-upstream"))
SPEC = importlib.util.spec_from_file_location("android_build", ROOT / "tools" / "android_build.py")
android_build = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(android_build)


class AndroidBuildChecks(unittest.TestCase):
    def test_apk_identity_version_and_certificate_checks(self):
        from verify_android_apk import verify_identity
        metadata = {"application_id": "io.github.sunyink.mfabd2", "version_code": 501,
                    "version_name": "v4.3.19-beta.260909.abcdef", "certificate_sha256": "a" * 64}
        badging = "package: name='io.github.sunyink.mfabd2' versionCode='501' versionName='v4.3.19-beta.260909.abcdef'"
        signature = "Signer #1 certificate SHA-256 digest: " + "a" * 64
        verify_identity(metadata, badging, signature)
        for incorrect in (badging.replace("501", "401"), badging.replace("mfabd2", "other"), badging + "\napplication-debuggable"):
            with self.assertRaises(ValueError):
                verify_identity(metadata, incorrect, signature)
        with self.assertRaises(ValueError):
            verify_identity(metadata, badging, signature.replace("a" * 64, "b" * 64))

    def test_explicit_version_is_preserved_and_invalid_is_rejected(self):
        for value in ("v4.3.19", "v4.3.19-beta.260909.abcdef", "4.3.19-alpha.1+ci"):
            self.assertEqual(android_build.display_version(value, [], "sha", "260909", ""), value)
        for value in ("v4.3.19/foo", "v4.3.19\nextra"):
            with self.assertRaises(ValueError):
                android_build.display_version(value, [], "sha", "260909", "")

    def test_implicit_channels_follow_project_rules(self):
        tags = ["v4.3.17", "v4.3.18", "v4.3.18-beta.1"]
        self.assertEqual(android_build.display_version("", tags, "abcdef", "260909", ""), "v4.3.18-ci.260909.abcdef")
        self.assertEqual(android_build.display_version("", tags, "abcdef", "260909", "[deploy-beta]"), "v4.3.19-beta.260909.abcdef")
        self.assertEqual(android_build.display_version("", tags, "abcdef", "260909", "[deploy-alpha]"), "v4.3.20-alpha.260909.abcdef")

    def test_version_code_range_and_monotonic_sequence(self):
        values = [android_build.version_code(7, attempt) for attempt in range(1, 100)]
        self.assertEqual(values, sorted(values))
        self.assertLess(values[-1], android_build.version_code(8, 1))
        self.assertGreater(android_build.version_code(8, 1), values[-1])
        with self.assertRaises(ValueError):
            android_build.version_code(0, 1)
        with self.assertRaises(ValueError):
            android_build.version_code(1, 100)
        with self.assertRaises(ValueError):
            android_build.version_code(21_000_000, 1)

    def test_prepare_ui_pins_identity_abi_and_version_code(self):
        with tempfile.TemporaryDirectory() as work:
            upstream = Path(work) / "MaaFwApp"
            gradle = upstream / "build-logic/convention/src/main/kotlin/com/aliothmoon/maafw/gradle"
            gradle.mkdir(parents=True)
            for name in ("AndroidApplicationConventionPlugin.kt", "GitVersion.kt"):
                shutil.copy2(UPSTREAM / "build-logic/convention/src/main/kotlin/com/aliothmoon/maafw/gradle" / name, gradle / name)
            android_build.prepare_ui(upstream, {"application_id": "io.github.sunyink.mfabd2"}, 12345)
            app = (gradle / "AndroidApplicationConventionPlugin.kt").read_text(encoding="utf-8")
            version = (gradle / "GitVersion.kt").read_text(encoding="utf-8")
            self.assertIn('BASE_APPLICATION_ID = "io.github.sunyink"', app)
            self.assertIn('app.id', app)
            self.assertIn('SHIPPED_ABIS = listOf("arm64-v8a")', app)
            self.assertIn("return 12345", version)


if __name__ == "__main__":
    unittest.main(verbosity=2)
