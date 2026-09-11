"""Small regression checks for the Android release metadata helper."""

import importlib.util
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = Path(os.environ.get("MFABD2_ANDROID_UPSTREAM", ROOT / "android-upstream"))
SPEC = importlib.util.spec_from_file_location("android_build", ROOT / "scripts" / "android_build.py")
android_build = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(android_build)


class AndroidBuildChecks(unittest.TestCase):
    def test_apk_identity_version_and_certificate_checks(self):
        from verify_android_apk import verify_identity
        metadata = {"application_id": "io.github.sunyink.mfabd2", "version_code": 501, "label": "MFABD2",
                    "version_name": "v4.3.19-beta.260909.abcdef", "certificate_sha256": "a" * 64}
        badging = ("package: name='io.github.sunyink.mfabd2' versionCode='501' "
                   "versionName='v4.3.19-beta.260909.abcdef'\napplication-label:'MFABD2'")
        signature = "Signer #1 certificate SHA-256 digest: " + "a" * 64
        verify_identity(metadata, badging, signature)
        for incorrect in (badging.replace("501", "401"), badging.replace("mfabd2", "other"),
                          badging + "\napplication-debuggable",
                          # A CI build whose label never got rewritten would be
                          # indistinguishable from the real app on the home screen.
                          badging.replace("application-label:'MFABD2'", "application-label:'MaaFwApp'"),
                          badging.replace("\napplication-label:'MFABD2'", "")):
            with self.assertRaises(ValueError):
                verify_identity(metadata, incorrect, signature)
        with self.assertRaises(ValueError):
            verify_identity(metadata, badging, signature.replace("a" * 64, "b" * 64))
        # The CI identity must differ from the released app in both name and package.
        ci = {**metadata, "application_id": "io.github.sunyink.mfabd2.ci", "label": "MFABD2 Ci"}
        with self.assertRaises(ValueError):
            verify_identity(ci, badging, signature)

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

    def test_channels_share_sequence_and_older_rerun_keeps_its_slot(self):
        versions = ["v4.4.2-alpha.260909.fffffff", "v4.4.1-beta.260909.aaaaaaa", "v4.4.0"]
        codes = [android_build.version_code(run, 1) for run in range(7, 10)]
        self.assertEqual(codes, [701, 801, 901])
        # The sequence number is deliberately absent from the filename; only the
        # identity shows up there, so the release asset matches the desktop zips.
        for version in versions:
            self.assertEqual(android_build.apk_name(version), f"MFABD2-{version}-android-arm64.apk")
            self.assertEqual(android_build.apk_name(version, android_build.CI),
                             f"MFABD2-{version}-android-arm64-ci.apk")
        with self.assertRaises(ValueError):
            android_build.apk_name("v4.4.0", "debug")
        self.assertLess(android_build.version_code(7, 99), codes[-1])
        self.assertGreater(android_build.version_code(1, 1, offset=1000), codes[-1])
        for args in ((1, 0), (1, -1), (1, 1, -1), (True, 1), (1, 1.5)):
            with self.assertRaises(ValueError):
                android_build.version_code(*args)
        self.assertEqual(android_build.version_code(20_999_999, 99, 1), 2_100_000_000)

    def test_published_triplet_agrees_on_name_code_and_digest(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            apk = root / "app.apk"
            apk.write_bytes(b"signed APK fixture")
            metadata = {"identity": android_build.RELEASE, "version_name": "v4.4.0",
                        "version_code": 901, "apk_name": android_build.apk_name("v4.4.0")}
            output = root / "artifacts"
            android_build.finalize_artifacts(apk, metadata, output)
            name = metadata["apk_name"]
            manifest = json.loads((output / f"{name}.json").read_text())
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            self.assertEqual(manifest, {**metadata, "schema_version": 1, "apk_sha256": digest})
            self.assertEqual((output / name).read_bytes(), apk.read_bytes())
            self.assertEqual((output / f"{name}.sha256").read_text(), f"{digest}  {name}\n")
            # The sequence number left the filename, so a wrong one must still be
            # caught — by range checking, and by the identity/name disagreeing.
            for broken in ({"version_code": 0}, {"version_code": "901"},
                           {"identity": android_build.CI}):
                with self.assertRaises(ValueError):
                    android_build.finalize_artifacts(apk, {**metadata, **broken}, output)

    def test_requirements_follow_the_desktop_list(self):
        overrides = {"numpy": "==2.3.2", "Pillow": "==11.0.0", "StrEnum": "==0.4.15"}
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "requirements.txt"
            source.write_text("requests>=2.25.0\nnumpy<2\npillow>=9.0.0\n\n# MFAA_TAG=v2.15.2\n",
                              encoding="utf-8")
            text = android_build.android_requirements(source, overrides, "5.12.3")
        entries = [line for line in text.splitlines() if not line.startswith("#")]
        # Desktop constraints survive; only platform-forced packages are rewritten,
        # and the match is case-insensitive so `pillow` still hits the `Pillow` rule.
        self.assertEqual(entries, ["maafw==5.12.3", "requests>=2.25.0", "numpy==2.3.2",
                                   "pillow==11.0.0", "StrEnum==0.4.15"])

    def test_requirements_reject_unresolved_framework_version(self):
        for value in ("", "latest", "v5"):
            with self.assertRaises(ValueError):
                android_build.android_requirements(ROOT / "requirements.txt", {}, value)

    def test_real_requirements_carry_every_desktop_package(self):
        settings = json.loads((ROOT / "android/release.json").read_text(encoding="utf-8"))
        text = android_build.android_requirements(ROOT / "requirements.txt",
                                                  settings["python_requirements"], "5.12.3")
        produced = {android_build.normalize(android_build.PACKAGE.match(line).group(1))
                    for line in text.splitlines() if not line.startswith("#")}
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                self.assertIn(android_build.normalize(android_build.PACKAGE.match(entry).group(1)),
                              produced)
        self.assertIn("maafw", produced)

    def test_profile_app_id_carves_out_the_ci_identity(self):
        """Upstream has no applicationIdSuffix, so the CI package name comes from here."""
        for identity, expected in ((android_build.RELEASE, "  id: mfabd2"),
                                   (android_build.CI, "  id: mfabd2.ci")):
            with tempfile.TemporaryDirectory() as work:
                profile = Path(work) / "pi-profile.yaml"
                shutil.copy2(ROOT / "android/pi-profile.yaml", profile)
                android_build.apply_identity_to_profile(profile, "mfabd2", identity)
                self.assertIn(expected, profile.read_text(encoding="utf-8"))
        # A profile whose anchor moved must stop the build rather than ship the
        # wrong package name under the right label.
        with tempfile.TemporaryDirectory() as work:
            profile = Path(work) / "pi-profile.yaml"
            profile.write_text("app:\n  id: something-else\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                android_build.apply_identity_to_profile(profile, "mfabd2", android_build.CI)

    def test_prepare_ui_pins_identity_abi_and_version_code(self):
        with tempfile.TemporaryDirectory() as work:
            profile = Path(work) / "pi-profile.yaml"
            shutil.copy2(ROOT / "android/pi-profile.yaml", profile)
            upstream = Path(work) / "MaaFwApp"
            gradle = upstream / "build-logic/convention/src/main/kotlin/com/aliothmoon/maafw/gradle"
            gradle.mkdir(parents=True)
            for name in ("AndroidApplicationConventionPlugin.kt", "GitVersion.kt"):
                shutil.copy2(UPSTREAM / "build-logic/convention/src/main/kotlin/com/aliothmoon/maafw/gradle" / name, gradle / name)
            module = upstream / "app/src/main/java/com/aliothmoon/maafw/di/UpdateModule.kt"
            module.parent.mkdir(parents=True)
            shutil.copy2(UPSTREAM / "app/src/main/java/com/aliothmoon/maafw/di/UpdateModule.kt", module)
            preferences = upstream / "app/src/main/java/com/aliothmoon/maafw/settings/AppSettings.kt"
            preferences.parent.mkdir(parents=True)
            shutil.copy2(UPSTREAM / "app/src/main/java/com/aliothmoon/maafw/settings/AppSettings.kt", preferences)
            android_build.prepare_ui(upstream, {"application_id": "io.github.sunyink.mfabd2",
                                                "certificate_sha256": "a" * 64},
                                     12345, android_build.RELEASE, profile)
            app = (gradle / "AndroidApplicationConventionPlugin.kt").read_text(encoding="utf-8")
            version = (gradle / "GitVersion.kt").read_text(encoding="utf-8")
            self.assertIn('BASE_APPLICATION_ID = "io.github.sunyink"', app)
            self.assertIn('app.id', app)
            self.assertIn('SHIPPED_ABIS = listOf("arm64-v8a")', app)
            self.assertIn("return 12345", version)
            self.assertIn("Mfabd2GitHubUpdateClient(get(), get())", module.read_text())
            self.assertIn('val updateSource: String = "GITHUB"', preferences.read_text(encoding="utf-8"))
            policy = upstream / "app/src/main/java/com/aliothmoon/maafw/update/Mfabd2ReleasePolicy.kt"
            self.assertIn('CERTIFICATE_SHA256 = "' + "a" * 64 + '"', policy.read_text())
            self.assertTrue((upstream / "app/src/test/java/com/aliothmoon/maafw/update/Mfabd2GitHubUpdateClientTest.kt").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
