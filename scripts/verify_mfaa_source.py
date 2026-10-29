"""Contract tests for mfaa_source.py using only the Python standard library."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import mfaa_source


SHA = "00000a" + "1" * 34
TAG = "v2.16.2-mfbd2.g00000a"


def make_manifest(tag=TAG, sequence=None):
    packages = []
    for rid in mfaa_source.RIDS:
        package_name = f"MFAAvalonia-{tag}-{rid}" + (".zip" if rid.startswith("win-") else ".tar.gz")
        packages.append({"name": package_name, "size": 100 + len(rid), "sha256": hashlib.sha256(rid.encode()).hexdigest()})
    manifest = {
        "schema": 1 if sequence is None else 2,
        "repository": mfaa_source.FORK,
        "branch": mfaa_source.FORK_BRANCH,
        "commit": SHA,
        "short_commit": SHA[:6],
        "upstream_tag": "v2.16.2",
        "tag": tag,
        "packages": packages,
    }
    if sequence is not None:
        manifest.pop("upstream_tag")
        manifest.update(fork_sequence=sequence, upstream_version="v2.16.2")
    return manifest


def make_release(manifest=None):
    manifest = manifest or make_manifest()
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    assets = [
        {"name": package["name"], "size": package["size"],
         "state": "uploaded", "digest": f"sha256:{package['sha256']}"}
        for package in manifest["packages"]
    ]
    assets.append({"id": 123, "name": mfaa_source.MANIFEST, "size": len(raw),
                   "state": "uploaded", "digest": f"sha256:{hashlib.sha256(raw).hexdigest()}"})
    return {"tag_name": manifest["tag"], "target_commitish": SHA,
            "prerelease": True, "draft": False, "assets": assets}, raw


class ResolveSourceTests(unittest.TestCase):
    def test_empty_overrides_select_upstream_without_fetch_or_download(self):
        for override in ("", "  \t\n"):
            fetch = mock.Mock(side_effect=AssertionError("fetch must not run"))
            download = mock.Mock(side_effect=AssertionError("download must not run"))
            with self.subTest(override=repr(override)):
                self.assertEqual(mfaa_source.resolve_source("v2.16.2", override, fetch, download),
                                 {"repository": mfaa_source.UPSTREAM, "tag": "v2.16.2", "commit": ""})
                fetch.assert_not_called()
                download.assert_not_called()

    def test_invalid_or_shell_like_overrides_are_rejected_without_network(self):
        for override in ("abcde", "abcdef0", "ABCDEF!", "abcdef;echo", "abcdef$(id)"):
            fetch = mock.Mock(side_effect=AssertionError("fetch must not run"))
            download = mock.Mock(side_effect=AssertionError("download must not run"))
            with self.subTest(override=override), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", override, fetch, download)
            fetch.assert_not_called()
            download.assert_not_called()

    def resolver(self, *, manifest=None, releases=None, release_edits=None,
                 manifest_asset_edits=None, tag_commit=SHA):
        manifest = manifest or make_manifest()
        release, raw = make_release(manifest)
        if release_edits:
            release_edits(release)
        if manifest_asset_edits:
            manifest_asset_edits(release["assets"][-1])
        if releases is None:
            releases = [release]

        def fetch(path):
            if path == f"repos/{mfaa_source.FORK}/commits/00000a":
                return {"sha": SHA}
            if path == f"repos/{mfaa_source.FORK}/releases?per_page=100&page=1":
                return releases
            if path == f"repos/{mfaa_source.FORK}/commits/{release['tag_name']}":
                return {"sha": tag_commit}
            raise AssertionError(f"Unexpected fetch path: {path}")

        return fetch, mock.Mock(return_value=raw), release

    def test_resolves_matching_fork_release_and_normalizes_uppercase_short_sha(self):
        fetch, download, _ = self.resolver()
        result = mfaa_source.resolve_source("v2.16.2", "00000A", fetch, download)
        self.assertEqual(result, {"repository": mfaa_source.FORK, "tag": TAG, "commit": SHA})
        download.assert_called_once_with(123)

    def test_resolves_numbered_release_using_its_exact_source_commit(self):
        for sequence in (6, 10):
            tag = f"v2.16.2-MFABD2.beta.{sequence}"
            fetch, download, _ = self.resolver(manifest=make_manifest(tag, sequence))
            with self.subTest(sequence=sequence):
                self.assertEqual(mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download),
                                 {"repository": mfaa_source.FORK, "tag": tag, "commit": SHA})

    def test_numbered_release_for_another_source_is_not_selected(self):
        manifest = make_manifest("v2.16.2-MFABD2.beta.6", 6)
        fetch, download, _ = self.resolver(manifest=manifest,
            release_edits=lambda release: release.update(target_commitish="b" * 40))
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)
        download.assert_not_called()

    def test_numbered_identity_rejects_wrong_schema_counter_baseline_and_source(self):
        for key, value in (("schema", 1), ("fork_sequence", 5), ("fork_sequence", True),
                           ("fork_sequence", 0), ("upstream_version", "v2.16.2-beta.1"),
                           ("upstream_version", "v2.16.3"), ("short_commit", "bbbbbb")):
            manifest = make_manifest("v2.16.2-MFABD2.beta.6", 6)
            manifest[key] = value
            fetch, download, _ = self.resolver(manifest=manifest)
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

    def test_historical_package_can_be_selected_through_a_numbered_release(self):
        tag = "v2.16.2-MFABD2.beta.3"
        fetch, download, _ = self.resolver(release_edits=lambda release: release.update(tag_name=tag))
        self.assertEqual(mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download),
                         {"repository": mfaa_source.FORK, "tag": tag, "commit": SHA})

    def test_historical_package_cannot_be_reassigned_to_another_base_or_reserved_number(self):
        for tag, sequence in (("v2.17.0-MFABD2.beta.3", None), ("v2.16.2-MFABD2.beta.3", 5)):
            manifest = make_manifest()
            if sequence is not None:
                manifest["fork_sequence"] = sequence
            fetch, download, _ = self.resolver(manifest=manifest,
                release_edits=lambda release: release.update(tag_name=tag))
            with self.subTest(tag=tag, sequence=sequence), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

    def test_missing_duplicate_non_prerelease_and_wrong_target_releases_are_rejected(self):
        fetch, download, release = self.resolver(releases=[])
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

        fetch, download, release = self.resolver(releases=[release, dict(release)])
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

        def official(release):
            release["prerelease"] = False

        fetch, download, _ = self.resolver(release_edits=official)
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

        fetch, download, _ = self.resolver(tag_commit="f" * 40)
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

    def test_manifest_digest_and_length_mismatches_are_rejected(self):
        for edit in (
            lambda asset: asset.update(digest="sha256:" + "0" * 64),
            lambda asset: asset.update(size=asset["size"] + 1),
        ):
            fetch, download, _ = self.resolver(manifest_asset_edits=edit)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

    def test_manifest_identity_and_platform_package_mismatches_are_rejected(self):
        for key, value in (("repository", "MaaXYZ/MFAAvalonia"), ("branch", "master"),
                           ("commit", "f" * 40)):
            manifest = make_manifest()
            manifest[key] = value
            fetch, download, _ = self.resolver(manifest=manifest)
            with self.subTest(key=key), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

        missing = make_manifest()
        missing["packages"].pop()
        fetch, download, _ = self.resolver(manifest=missing)
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

        duplicate = make_manifest()
        duplicate["packages"][-1] = dict(duplicate["packages"][0])
        fetch, download, _ = self.resolver(manifest=duplicate)
        with self.assertRaises(ValueError):
            mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

    def test_platform_asset_digest_and_size_mismatches_are_rejected(self):
        for field, value in (("digest", "sha256:" + "0" * 64), ("size", 999)):
            def edit(release, field=field, value=value):
                release["assets"][0][field] = value

            fetch, download, _ = self.resolver(release_edits=edit)
            with self.subTest(field=field), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)

    def test_missing_and_duplicate_platform_attachments_are_rejected(self):
        def missing(release):
            release["assets"].pop(0)

        def duplicate(release):
            release["assets"].append(dict(release["assets"][0]))

        for edit in (missing, duplicate):
            fetch, download, _ = self.resolver(release_edits=edit)
            with self.subTest(edit=edit.__name__), self.assertRaises(ValueError):
                mfaa_source.resolve_source("v2.16.2", "00000a", fetch, download)


class VerifyExtractedTests(unittest.TestCase):
    def write_stamp(self, root, **changes):
        stamp = dict(schema=1, repository=mfaa_source.FORK, branch=mfaa_source.FORK_BRANCH,
                     commit=SHA, short_commit=SHA[:6], tag=TAG, rid="linux-x64")
        stamp.update(changes)
        (root / mfaa_source.STAMP).write_text(json.dumps(stamp), encoding="utf-8")

    def test_single_matching_stamp_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_stamp(root)
            with mock.patch("builtins.print"):
                mfaa_source.verify_extracted(root, TAG, SHA, "linux-x64")

    def test_numbered_package_and_unchanged_historical_package_both_verify(self):
        tag = "v2.16.2-MFABD2.beta.6"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_stamp(root, schema=2, upstream_version="v2.16.2", fork_sequence=6, tag=tag)
            with mock.patch("builtins.print"):
                mfaa_source.verify_extracted(root, tag, SHA, "linux-x64")
            self.write_stamp(root)
            with mock.patch("builtins.print"):
                mfaa_source.verify_extracted(root, "v2.16.2-MFABD2.beta.3", SHA, "linux-x64")

    def test_extracted_numbered_stamp_cannot_claim_another_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_stamp(root, schema=2, upstream_version="v2.16.2", fork_sequence=6,
                             tag="v2.16.2-MFABD2.beta.6")
            with self.assertRaises(ValueError):
                mfaa_source.verify_extracted(root, "v2.16.2-MFABD2.beta.7", SHA, "linux-x64")

    def test_missing_multiple_wrong_rid_and_wrong_commit_stamps_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                mfaa_source.verify_extracted(root, TAG, SHA, "linux-x64")

            self.write_stamp(root)
            nested = root / "nested"
            nested.mkdir()
            self.write_stamp(nested)
            with self.assertRaises(ValueError):
                mfaa_source.verify_extracted(root, TAG, SHA, "linux-x64")

        for changes in ({"rid": "win-x64"}, {"commit": "f" * 40}):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.write_stamp(root, **changes)
                with self.assertRaises(ValueError):
                    mfaa_source.verify_extracted(root, TAG, SHA, "linux-x64")


if __name__ == "__main__":
    unittest.main()
