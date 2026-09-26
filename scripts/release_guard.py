"""Keep releases private until every expected asset has been uploaded intact."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DESKTOP_TARGETS = (
    "win-x86_64", "win-aarch64",
    "macos-x86_64", "macos-aarch64",
    "linux-x86_64", "linux-aarch64",
)
# GitHub listings can lag behind writes; recheck a few times before failing.
VERIFY_ATTEMPTS = 3
VERIFY_DELAY = 10


class ReleaseError(RuntimeError):
    pass


def expected_assets(tag: str, identity: str) -> set[str]:
    if not tag or any(character in tag for character in "/\\\r\n"):
        raise ReleaseError("Invalid release tag")
    if identity not in ("ci", "release"):
        raise ReleaseError(f"Unknown Android identity: {identity}")
    suffix = "-ci" if identity == "ci" else ""
    apk = f"MFABD2-{tag}-android-arm64{suffix}.apk"
    return {f"MFABD2-{tag}-{target}.zip" for target in DESKTOP_TARGETS} | {
        "CHANGES.zip", apk, f"{apk}.json", f"{apk}.sha256",
    }


def local_assets(directory: Path, tag: str, identity: str) -> dict:
    expected = expected_assets(tag, identity)
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise ReleaseError(
            f"Release files differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    assets = {}
    for name in sorted(expected):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ReleaseError(f"Release asset is not a nonempty regular file: {name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        assets[name] = {"size": path.stat().st_size, "digest": f"sha256:{digest.hexdigest()}"}
    return assets


class GitHub:
    def __init__(self, repository: str, token: str):
        base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.base = f"{base}/repos/{repository}"
        self.token = token

    def request(self, method: str, endpoint: str, body=None):
        request = Request(
            f"{self.base}/{endpoint}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "MFABD2-release-guard",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read()
                # DELETE answers 204 with an empty body.
                return json.loads(payload) if payload else {}
        except HTTPError as error:
            raise ReleaseError(f"GitHub {method} {endpoint} failed: HTTP {error.code}") from error

    def listing(self, endpoint: str) -> list:
        items = []
        page = 1
        while True:
            batch = self.request("GET", f"{endpoint}?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ReleaseError(f"Expected an array from GitHub: {endpoint}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


def require_draft(release: dict, tag: str, source_sha: str) -> None:
    if release.get("tag_name") != tag or release.get("target_commitish") != source_sha:
        raise ReleaseError("Release tag/source does not match this build")
    if release.get("draft") is not True:
        raise ReleaseError("Release is already public; refusing to overwrite published assets")


def prepare(api: GitHub, directory: Path, tag: str, identity: str, source_sha: str) -> None:
    assets = local_assets(directory, tag, identity)
    # The by-tag endpoint returns 404 for drafts, even to the repository owner.
    matching = [release for release in api.listing("releases") if release.get("tag_name") == tag]
    if len(matching) > 1:
        raise ReleaseError(f"Multiple releases exist for {tag}; resolve them before retrying")
    for release in matching:
        require_draft(release, tag, source_sha)
    print(f"Verified {len(assets)} local assets; safe to upload to a draft")


def verify_uploaded_assets(expected: dict, uploaded: list) -> None:
    names = [asset.get("name") for asset in uploaded]
    if len(names) != len(expected) or set(names) != set(expected):
        raise ReleaseError("Uploaded assets do not exactly match the expected release files")
    for asset in uploaded:
        name = asset["name"]
        if (asset.get("state") != "uploaded"
                or asset.get("size") != expected[name]["size"]
                or asset.get("digest") != expected[name]["digest"]):
            raise ReleaseError(f"Uploaded asset is incomplete or has a different SHA-256: {name}")


def publish(api: GitHub, directory: Path, tag: str, identity: str, source_sha: str,
            release_id: str, prerelease: bool) -> None:
    if not release_id.isdecimal():
        raise ReleaseError("Missing or invalid draft release ID")
    endpoint = f"releases/{quote(release_id, safe='')}"
    expected = local_assets(directory, tag, identity)
    release = api.request("GET", endpoint)
    require_draft(release, tag, source_sha)
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        try:
            verify_uploaded_assets(expected, api.listing(f"{endpoint}/assets"))
            break
        except (ReleaseError, OSError) as error:
            if attempt == VERIFY_ATTEMPTS:
                raise
            print(f"Asset check {attempt}/{VERIFY_ATTEMPTS} failed ({error}); retrying")
            time.sleep(VERIFY_DELAY)
    # Recheck immediately before the only operation that makes the release public.
    require_draft(api.request("GET", endpoint), tag, source_sha)
    published = api.request("PATCH", endpoint, {"draft": False, "prerelease": prerelease})
    if (published.get("id") != int(release_id) or published.get("tag_name") != tag
            or published.get("draft") is not False or published.get("prerelease") is not prerelease):
        raise ReleaseError("GitHub did not confirm the expected publication state")
    print(f"Published {tag}: prerelease={prerelease}, verified assets={len(expected)}")


def cleanup(api: GitHub, tag: str, source_sha: str) -> None:
    """Delete this build's unpublished draft after the release job failed or was cancelled."""
    for listed in api.listing("releases"):
        if not tag or listed.get("tag_name") != tag:
            continue
        # The listing may be stale; only the release read by ID decides what gets deleted.
        release = api.request("GET", f"releases/{int(listed['id'])}")
        if release.get("draft") is not True:
            print(f"{tag} is public; left untouched")
        elif release.get("tag_name") != tag or release.get("target_commitish") != source_sha:
            print(f"::warning::Draft {release.get('id')} does not match {tag}@{source_sha}; left untouched")
        else:
            api.request("DELETE", f"releases/{int(release['id'])}")
            print(f"Deleted unpublished draft {release['id']} for {tag}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "publish", "cleanup"))
    parser.add_argument("--assets", type=Path, default=Path("artifacts/release_assets"))
    args = parser.parse_args()
    try:
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        if args.command == "cleanup":
            cleanup(api, os.environ["RELEASE_TAG"], os.environ["SOURCE_SHA"])
            return 0
        common = (api, args.assets, os.environ["RELEASE_TAG"], os.environ["ANDROID_IDENTITY"],
                  os.environ["SOURCE_SHA"])
        if args.command == "prepare":
            prepare(*common)
        else:
            flag = os.environ["RELEASE_PRERELEASE"]
            if flag not in ("true", "false"):
                raise ReleaseError("RELEASE_PRERELEASE must be true or false")
            publish(*common, os.environ["RELEASE_ID"], flag == "true")
    except (ReleaseError, OSError, URLError, ValueError, KeyError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
