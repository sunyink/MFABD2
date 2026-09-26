"""Offline regression checks for incomplete/cancelled release publication."""

import copy
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import release_guard
from release_guard import (
    DESKTOP_TARGETS, GitHub, ReleaseError, cleanup, expected_assets, local_assets, prepare, publish,
)


TAG = "v4.4.5-beta.260925.49496bfd"
SHA = "49496bfd99d8eef6f5696801f7c2c5f8361f38d4"
ROOT = Path(__file__).resolve().parents[1]
release_guard.VERIFY_DELAY = 0


class FakeGitHub:
    def __init__(self, assets):
        self.release = {"id": 519, "tag_name": TAG, "target_commitish": SHA, "draft": True}
        self.assets = [{"name": name, "state": "uploaded", **metadata}
                       for name, metadata in assets.items()]
        self.releases = [self.release]
        self.patches = []
        self.deletes = []
        self.fail_listing = False
        self.stale_asset_reads = 0
        self.reads = 0
        self.change_before_publish = False

    def listing(self, endpoint):
        if self.fail_listing:
            raise ReleaseError("Simulated GitHub API failure")
        if endpoint == "releases":
            return self.releases
        if self.stale_asset_reads:
            self.stale_asset_reads -= 1
            return self.assets[1:]
        return self.assets

    def request(self, method, endpoint, body=None):
        if method == "GET":
            self.reads += 1
            if self.change_before_publish and self.reads == 2:
                self.release["draft"] = False
            return copy.deepcopy(self.release)
        if method == "DELETE":
            self.deletes.append(endpoint)
            return {}
        assert method == "PATCH"
        self.patches.append(body)
        return {**self.release, **body}


class ReleaseGuardChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in expected_assets(TAG, "release"):
            (self.directory / name).write_bytes(name.encode())
        self.expected = local_assets(self.directory, TAG, "release")
        self.api = FakeGitHub(self.expected)

    def publish(self, prerelease=True, release_id="519"):
        publish(self.api, self.directory, TAG, "release", SHA, release_id, prerelease)

    def assert_blocked(self):
        with self.assertRaises(ReleaseError):
            self.publish()
        self.assertEqual(self.api.patches, [])

    def test_complete_draft_can_be_published_as_pre_or_stable(self):
        for prerelease in (True, False):
            with self.subTest(prerelease=prerelease):
                self.api = FakeGitHub(self.expected)
                self.publish(prerelease)
                self.assertEqual(self.api.patches, [{"draft": False, "prerelease": prerelease}])

    def test_issue_519_missing_uploaded_windows_x64_is_blocked(self):
        self.api.assets = [asset for asset in self.api.assets if not asset["name"].endswith("win-x86_64.zip")]
        self.assert_blocked()

    def test_partial_upload_size_digest_and_state_are_blocked(self):
        for key, value in (("size", 0), ("size", 1), ("digest", None),
                           ("digest", "sha256:" + "0" * 64), ("state", "starter")):
            with self.subTest(key=key, value=value):
                self.api = FakeGitHub(self.expected)
                self.api.assets[0][key] = value
                self.assert_blocked()

    def test_duplicate_or_unexpected_remote_asset_is_blocked(self):
        for duplicate in (True, False):
            self.api = FakeGitHub(self.expected)
            extra = copy.deepcopy(self.api.assets[0])
            if not duplicate:
                extra["name"] = "unrelated.zip"
            self.api.assets.append(extra)
            self.assert_blocked()

    def test_wrong_release_or_public_release_is_blocked(self):
        for key, value in (("draft", False), ("tag_name", "v0.0.0"), ("target_commitish", "other")):
            with self.subTest(key=key):
                self.api = FakeGitHub(self.expected)
                self.api.release[key] = value
                self.assert_blocked()

    def test_api_failure_or_concurrent_publication_does_not_patch(self):
        self.api.fail_listing = True
        self.assert_blocked()
        self.api = FakeGitHub(self.expected)
        self.api.change_before_publish = True
        self.assert_blocked()

    def test_missing_action_output_is_blocked(self):
        with self.assertRaises(ReleaseError):
            self.publish(release_id="")
        self.assertEqual(self.api.patches, [])

    def test_missing_local_platform_is_blocked_before_upload(self):
        (self.directory / f"MFABD2-{TAG}-win-x86_64.zip").unlink()
        with self.assertRaisesRegex(ReleaseError, "missing="):
            prepare(self.api, self.directory, TAG, "release", SHA)

    def test_empty_or_extra_local_file_is_blocked(self):
        path = self.directory / "CHANGES.zip"
        original = path.read_bytes()
        path.write_bytes(b"")
        with self.assertRaisesRegex(ReleaseError, "nonempty"):
            local_assets(self.directory, TAG, "release")
        path.write_bytes(original)
        (self.directory / "old-release.zip").write_bytes(b"stale")
        with self.assertRaisesRegex(ReleaseError, "unexpected="):
            local_assets(self.directory, TAG, "release")

    def test_prepare_allows_new_or_same_source_draft_but_not_public_or_duplicate(self):
        prepare(self.api, self.directory, TAG, "release", SHA)
        self.api.releases = []
        prepare(self.api, self.directory, TAG, "release", SHA)
        self.api.releases = [self.api.release, self.api.release]
        with self.assertRaisesRegex(ReleaseError, "Multiple"):
            prepare(self.api, self.directory, TAG, "release", SHA)
        self.api.releases = [self.api.release]
        self.api.release["draft"] = False
        with self.assertRaisesRegex(ReleaseError, "already public"):
            prepare(self.api, self.directory, TAG, "release", SHA)
        self.assertEqual(self.api.patches, [])

    def test_ci_identity_uses_three_ci_apk_files(self):
        names = expected_assets("v4.4.4-ci.260926.12345678", "ci")
        self.assertEqual(len(names), 10)
        self.assertEqual(len([name for name in names if "-android-arm64-ci.apk" in name]), 3)
        self.assertEqual(len(self.expected), 10)
        with self.assertRaises(ReleaseError):
            expected_assets(TAG, "unknown")

    def test_cli_failure_returns_one(self):
        (self.directory / "CHANGES.zip").unlink()
        environment = dict(os.environ, GITHUB_REPOSITORY="example/test", GH_TOKEN="unused",
                           RELEASE_TAG=TAG, ANDROID_IDENTITY="release", SOURCE_SHA=SHA)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/release_guard.py"), "prepare",
             "--assets", str(self.directory)], env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::", result.stderr)

    def test_github_listing_reads_all_pages(self):
        api = GitHub("example/test", "unused")
        endpoints = []

        def request(method, endpoint):
            endpoints.append(endpoint)
            return [{}] * (100 if endpoint.endswith("page=1") else 1)

        api.request = request
        self.assertEqual(len(api.listing("releases")), 101)
        self.assertEqual(len(endpoints), 2)

    def test_empty_response_body_is_accepted(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b""
        with mock.patch.object(release_guard, "urlopen", return_value=response):
            self.assertEqual(GitHub("example/test", "unused").request("DELETE", "releases/1"), {})

    def test_lagging_asset_listing_is_retried_but_not_forever(self):
        self.api.stale_asset_reads = 1
        self.publish()
        self.assertEqual(len(self.api.patches), 1)
        self.api = FakeGitHub(self.expected)
        self.api.stale_asset_reads = release_guard.VERIFY_ATTEMPTS
        self.assert_blocked()

    def test_cleanup_deletes_this_builds_unpublished_draft(self):
        cleanup(self.api, TAG, SHA)
        self.assertEqual(self.api.deletes, ["releases/519"])

    def test_cleanup_leaves_public_foreign_or_unrelated_releases_untouched(self):
        for label, change in (("public, stale listing says draft", {"draft": False}),
                              ("different source", {"target_commitish": "other"})):
            with self.subTest(label):
                self.api = FakeGitHub(self.expected)
                self.api.releases = [copy.deepcopy(self.api.release)]
                self.api.release.update(change)
                cleanup(self.api, TAG, SHA)
                self.assertEqual(self.api.deletes, [])
        for tag in ("v0.0.0", ""):
            with self.subTest(tag=tag):
                self.api = FakeGitHub(self.expected)
                cleanup(self.api, tag, SHA)
                self.assertEqual(self.api.deletes, [])


class WorkflowChecks(unittest.TestCase):
    def test_publication_order_and_failure_behavior(self):
        workflow = (ROOT / ".github/workflows/install-build.yml").read_text(encoding="utf-8")
        release = workflow.split("\n  release:\n", 1)[1].split("\n  notify:\n", 1)[0]
        order = [release.index(marker) for marker in (
            "scripts/release_guard.py prepare", "id: draft_release",
            "scripts/release_guard.py publish", "name: Trigger MirrorChyanUploading",
            "name: Remove unpublished draft",
        )]
        self.assertEqual(order, sorted(order))
        cleanup_step = release.split("name: Remove unpublished draft", 1)[1]
        self.assertIn("if: failure() || cancelled()", cleanup_step)
        self.assertIn("scripts/release_guard.py cleanup", cleanup_step)
        self.assertIn("draft: true", release)
        self.assertNotIn("draft: false", release)
        self.assertNotIn("continue-on-error:", release)
        self.assertNotIn("always()", release)
        self.assertIn("needs: [meta, install, android, changelog]", release)
        self.assertRegex(release, r"defaults:\s+run:\s+(?:#[^\n]*\n\s+)?shell: bash")

    def test_original_prerelease_policy_is_preserved_and_shared(self):
        workflow = (ROOT / ".github/workflows/install-build.yml").read_text(encoding="utf-8")
        original = (
            "${{ contains(needs.meta.outputs.tag, '-beta') || contains(needs.meta.outputs.tag, '-alpha') "
            "|| (contains(needs.meta.outputs.tag, '-ci') && !(github.event_name == 'workflow_dispatch' "
            "&& github.event.inputs.ci_as_stable == 'true')) }}"
        )
        self.assertIn(f"RELEASE_PRERELEASE: {original}", workflow)
        self.assertIn("prerelease: ${{ env.RELEASE_PRERELEASE == 'true' }}", workflow)

    def test_expected_desktop_assets_cover_the_build_matrix(self):
        workflow = (ROOT / ".github/workflows/install-build.yml").read_text(encoding="utf-8")
        matrix = re.findall(r"- os: (\w+)\s+arch: (\w+)", workflow)
        self.assertEqual({f"{system}-{arch}" for system, arch in matrix}, set(DESKTOP_TARGETS))


if __name__ == "__main__":
    unittest.main()
