"""Publish a complete, owned draft; never overwrite assets of a public release.

The hidden release-body record is also the recovery/cleanup receipt. Each writer
and cleanup job holds the workflow's per-tag concurrency lock. Python >= 3.11.
"""
import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DESKTOP_TARGETS = (
    "win-x86_64", "win-aarch64", "macos-x86_64", "macos-aarch64",
    "linux-x86_64", "linux-aarch64",
)
MARKER = "mfabd2-release-guard:v1"
MARKER_RE = re.compile(r"<!-- " + re.escape(MARKER) + r" ([A-Za-z0-9_=-]+) -->")
FAILED_RUNS = {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}


class ReleaseError(RuntimeError):
    pass


class GitHubError(ReleaseError):
    def __init__(self, method, endpoint, status):
        self.status = status
        super().__init__(f"GitHub {method} {endpoint} failed: HTTP {status}")


def expected_assets(tag: str, identity: str) -> set[str]:
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}", tag):
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
        raise ReleaseError(f"Release files differ: missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}")
    assets = {}
    for name in sorted(expected):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ReleaseError(f"Release asset is not a nonempty regular file: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assets[name] = {"size": path.stat().st_size, "digest": f"sha256:{digest}"}
    return assets


@dataclass(frozen=True)
class Build:
    repository: str
    tag: str
    source_sha: str
    identity: str
    run_id: int
    attempt: int
    prerelease: bool

    def __post_init__(self):
        expected_assets(self.tag, self.identity)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ReleaseError("Invalid repository")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise ReleaseError("Source must be a full commit SHA")
        if type(self.run_id) is not int or self.run_id <= 0 or type(self.attempt) is not int or self.attempt <= 0:
            raise ReleaseError("Invalid workflow run/attempt")
        if type(self.prerelease) is not bool:
            raise ReleaseError("Invalid prerelease flag")

    def receipt(self, assets):
        return {"schema": 1, **self.__dict__, "assets": assets}


def read_receipt(release):
    if not isinstance(release, dict) or not isinstance(release.get("body"), str):
        raise ReleaseError("Release has no publication receipt")
    matches = MARKER_RE.findall(release.get("body") or "")
    if len(matches) != 1:
        raise ReleaseError("Release has no unique publication receipt; refusing to take ownership")
    try:
        value = json.loads(base64.urlsafe_b64decode(matches[0]).decode("utf-8"))
        build = Build(**{key: value[key] for key in Build.__dataclass_fields__})
        assets = value["assets"]
        if value.get("schema") != 1 or not isinstance(assets, dict) or set(assets) != expected_assets(build.tag, build.identity):
            raise ValueError("Invalid manifest")
        for meta in assets.values():
            if (not isinstance(meta, dict) or type(meta.get("size")) is not int or meta["size"] <= 0
                    or not isinstance(meta.get("digest"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", meta["digest"])):
                raise ValueError("Invalid asset digest/size")
        return build, assets
    except (ValueError, KeyError, TypeError) as error:
        raise ReleaseError("Invalid publication receipt") from error


def release_body(body, build, assets):
    if MARKER in body:
        raise ReleaseError("Release notes contain a reserved publication marker")
    encoded = base64.urlsafe_b64encode(json.dumps(build.receipt(assets), sort_keys=True).encode()).decode()
    marker = f"<!-- {MARKER} {encoded} -->"
    return body[:125000 - len(marker) - 2] + "\n\n" + marker


class GitHub:
    def __init__(self, repository: str, token: str):
        self.repository = repository
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.base = f"{api}/repos/{repository}"
        self.api_host = urlparse(api).hostname
        self.token = token

    def _request(self, method, endpoint, body=None, *, url=None, headers=None):
        request = Request(url or f"{self.base}/{endpoint}", data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "User-Agent": "MFABD2-release-guard",
            "X-GitHub-Api-Version": "2026-03-10", **(headers or {}),
        })
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read()
                try:
                    return json.loads(payload) if payload else None
                except ValueError as error:
                    raise ReleaseError(f"GitHub {method} {endpoint} returned invalid JSON") from error
        except HTTPError as error:
            raise GitHubError(method, endpoint, error.code) from error

    def request(self, method, endpoint, body=None):
        data = json.dumps(body).encode() if body is not None else None
        return self._request(method, endpoint, data)

    def optional(self, endpoint):
        try:
            return self.request("GET", endpoint)
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def listing(self, endpoint):
        items, page = [], 1
        while True:
            batch = self.request("GET", f"{endpoint}?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ReleaseError(f"Expected an array from GitHub: {endpoint}")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def upload(self, release, path):
        base = release["upload_url"].split("{", 1)[0]
        parsed = urlparse(base)
        if parsed.scheme != "https" or parsed.hostname not in {self.api_host, "uploads.github.com"}:
            raise ReleaseError("Unexpected asset upload host")
        url = f"{base}?name={quote(path.name, safe='')}"
        with path.open("rb") as stream:
            return self._request("POST", "asset upload", stream, url=url, headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size),
            })


def resolve_tag(api, tag):
    ref = api.optional(f"git/ref/tags/{quote(tag, safe='')}")
    if ref is None:
        return None
    obj = ref["object"]
    for _ in range(16):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api.request("GET", f"git/tags/{quote(obj['sha'], safe='')}")["object"]
    raise ReleaseError("Tag does not resolve to a commit")


def require_tag(api, build, *, create=False):
    actual = resolve_tag(api, build.tag)
    if actual is None and create:
        try:
            api.request("POST", "git/refs", {"ref": f"refs/tags/{build.tag}", "sha": build.source_sha})
        except (ReleaseError, URLError, OSError):
            # Creation may have succeeded even if its response was lost.
            if resolve_tag(api, build.tag) != build.source_sha:
                raise
        actual = resolve_tag(api, build.tag)
    if actual != build.source_sha:
        raise ReleaseError("Actual tag commit does not match this build")


def matching_releases(api, tag):
    return [item for item in api.listing("releases") if item.get("tag_name") == tag]


def require_unique(api, build, release_id):
    matches = matching_releases(api, build.tag)
    if len(matches) != 1 or matches[0].get("id") != int(release_id):
        raise ReleaseError("Release tag is not unique or the release ID changed")


def require_source(release, build):
    if not isinstance(release, dict):
        raise ReleaseError("GitHub returned no release object")
    if release.get("tag_name") != build.tag or release.get("target_commitish") != build.source_sha:
        raise ReleaseError("Release tag/source does not match this build")


def require_owned_draft(release, build, assets):
    require_source(release, build)
    owner, recorded = read_receipt(release)
    if release.get("draft") is not True or owner != build or recorded != assets:
        raise ReleaseError("Draft no longer belongs to this workflow attempt/manifest")


def verify_uploaded_assets(expected, uploaded):
    names = [asset.get("name") for asset in uploaded]
    if len(names) != len(expected) or set(names) != set(expected):
        raise ReleaseError("Uploaded assets do not exactly match the expected release files")
    for asset in uploaded:
        if (asset.get("state") != "uploaded" or asset.get("size") != expected[asset["name"]]["size"]
                or asset.get("digest") != expected[asset["name"]]["digest"]):
            raise ReleaseError(f"Uploaded asset is incomplete or has a different SHA-256: {asset['name']}")


def verify_public(api, release, build):
    require_source(release, build)
    owner, manifest = read_receipt(release)
    if (owner.repository, owner.tag, owner.source_sha, owner.identity, owner.prerelease) != (
            build.repository, build.tag, build.source_sha, build.identity, build.prerelease):
        raise ReleaseError("Published release has a different build identity")
    if release.get("draft") is not False or release.get("prerelease") is not build.prerelease:
        raise ReleaseError("Unexpected public release state")
    require_tag(api, build)
    require_unique(api, build, release["id"])
    verify_uploaded_assets(manifest, api.listing(f"releases/{release['id']}/assets"))


def verify_publication(api, release_id, tag, source_sha):
    """Read-only revalidation for independently rerunnable mirror dispatch."""
    release = api.request("GET", f"releases/{int(release_id)}")
    owner, _ = read_receipt(release)
    if (owner.repository, owner.tag, owner.source_sha) != (api.repository, tag, source_sha):
        raise ReleaseError("Publication does not match the requested source/tag")
    verify_public(api, release, owner)


def run_attempt(api, owner):
    run = api.request("GET", f"actions/runs/{owner.run_id}/attempts/{owner.attempt}")
    if (run.get("head_sha") != owner.source_sha or run.get("run_attempt") != owner.attempt
            or run.get("path", "").split("@")[0] != ".github/workflows/install.yml"):
        raise ReleaseError("Workflow attempt does not match draft ownership")
    return run


def prepare(api, directory, build, body=""):
    assets = local_assets(directory, build.tag, build.identity)
    matches = matching_releases(api, build.tag)
    if len(matches) > 1:
        raise ReleaseError("Multiple releases exist for this tag")
    if matches and matches[0].get("draft") is False:
        # ZIP timestamps can differ on a rerun. Verify the original published
        # manifest instead of overwriting it with freshly repackaged files.
        verify_public(api, matches[0], build)
        return matches[0]["id"], True
    if matches:
        existing = matches[0]
        require_source(existing, build)
        owner, _ = read_receipt(existing)
        if (owner.repository, owner.tag, owner.source_sha, owner.identity, owner.prerelease) != (
                build.repository, build.tag, build.source_sha, build.identity, build.prerelease):
            raise ReleaseError("Draft ownership/source differs")
        if owner.run_id == build.run_id and owner.attempt > build.attempt:
            raise ReleaseError("An older attempt cannot reclaim a newer attempt's draft")
        if owner != build:
            previous = run_attempt(api, owner)
            if previous.get("status") != "completed":
                raise ReleaseError("Another workflow attempt still owns this draft")
    require_tag(api, build, create=True)
    payload = {"tag_name": build.tag, "target_commitish": build.source_sha,
               "name": f"MFABD2 {build.tag}", "body": release_body(body, build, assets),
               "draft": True, "prerelease": build.prerelease}
    if matches:
        release = api.request("PATCH", f"releases/{existing['id']}", payload)
    else:
        release = api.request("POST", "releases", payload)
    require_owned_draft(release, build, assets)
    require_unique(api, build, release["id"])
    return release["id"], False


def upload(api, directory, build, release_id):
    expected = local_assets(directory, build.tag, build.identity)
    endpoint = f"releases/{int(release_id)}"
    require_tag(api, build)
    require_unique(api, build, release_id)
    require_owned_draft(api.request("GET", endpoint), build, expected)
    uploaded = api.listing(f"{endpoint}/assets")
    names = [item.get("name") for item in uploaded]
    if len(names) != len(set(names)) or set(names) - set(expected):
        raise ReleaseError("Unexpected/duplicate draft assets; refusing to delete arbitrary files")
    current = {item["name"]: item for item in uploaded}
    for name, metadata in expected.items():
        old = current.get(name)
        if old and all(old.get(key) == value for key, value in {"state": "uploaded", **metadata}.items()):
            continue
        release = api.request("GET", endpoint)
        require_owned_draft(release, build, expected)
        if old:
            api.request("DELETE", f"releases/assets/{old['id']}")
        # Ownership is checked again before each upload, always using this ID.
        release = api.request("GET", endpoint)
        require_owned_draft(release, build, expected)
        api.upload(release, directory / name)


def publish(api, directory, build, release_id):
    endpoint = f"releases/{int(release_id)}"
    release = api.request("GET", endpoint)
    require_source(release, build)
    if release.get("draft") is False:
        verify_public(api, release, build)
        print(f"Already published and intact: {build.tag}; assets left unchanged")
        return
    expected = local_assets(directory, build.tag, build.identity)
    require_owned_draft(release, build, expected)
    require_tag(api, build)
    require_unique(api, build, release_id)
    verify_uploaded_assets(expected, api.listing(f"{endpoint}/assets"))
    require_owned_draft(api.request("GET", endpoint), build, expected)
    try:
        published = api.request("PATCH", endpoint, {"draft": False, "prerelease": build.prerelease})
    except (ReleaseError, URLError, OSError):
        # Never blindly repeat a publication mutation with an unknown outcome.
        published = api.request("GET", endpoint)
        if published.get("draft") is not False:
            raise
    if published.get("id") != int(release_id):
        raise ReleaseError("GitHub returned a different release ID")
    verify_public(api, published, build)
    print(f"Published {build.tag}: verified assets={len(expected)}")


def cleanup_candidates(api, run_id, attempt, source_sha):
    candidates = []
    for release in api.listing("releases"):
        if release.get("draft") is not True:
            continue
        try:
            owner, _ = read_receipt(release)
        except ReleaseError:
            continue
        if (owner.repository, owner.run_id, owner.attempt, owner.source_sha) == (
                api.repository, run_id, attempt, source_sha):
            require_source(release, owner)
            candidates.append((release, owner))
    return candidates


def cleanup_target(api, run_id, attempt, source_sha):
    candidates = cleanup_candidates(api, run_id, attempt, source_sha)
    for _, owner in candidates:
        run = run_attempt(api, owner)
        if run.get("status") != "completed" or run.get("conclusion") not in FAILED_RUNS:
            raise ReleaseError("Cleanup requires a completed failed/cancelled workflow attempt")
    tags = {owner.tag for _, owner in candidates}
    if len(tags) > 1:
        raise ReleaseError("Unexpected multiple tags owned by one workflow attempt")
    return next(iter(tags), "")


def cleanup(api, run_id, attempt, source_sha, *, completed=False, tag=None):
    for candidate, owner in cleanup_candidates(api, run_id, attempt, source_sha):
        if tag is not None and owner.tag != tag:
            continue
        if completed:
            run = run_attempt(api, owner)
            if run.get("status") != "completed" or run.get("conclusion") not in FAILED_RUNS:
                continue
        current = api.optional(f"releases/{candidate['id']}")
        if current is None or current.get("draft") is not True or current.get("id") != candidate["id"]:
            continue
        try:
            current_owner, manifest = read_receipt(current)
        except ReleaseError:
            continue
        if current_owner != owner:
            continue  # A rerun adopted it while this cleanup was waiting for the tag lock.
        require_owned_draft(current, owner, manifest)
        api.request("DELETE", f"releases/{candidate['id']}")
        print(f"Deleted owned draft {candidate['id']} ({owner.tag}); Git tag retained")


def output(name, value):
    print(f"{name}={value}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "upload", "publish", "verify-public", "cleanup", "cleanup-target", "cleanup-completed"))
    parser.add_argument("--assets", type=Path, default=Path("artifacts/release_assets"))
    args = parser.parse_args()
    try:
        api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        if args.command == "verify-public":
            identifier = os.environ["RELEASE_ID"]
            if not identifier.isdecimal() or int(identifier) <= 0:
                raise ReleaseError("Invalid release ID")
            verify_publication(api, identifier, os.environ["RELEASE_TAG"], os.environ["SOURCE_SHA"])
            return 0
        if args.command.startswith("cleanup"):
            external = args.command != "cleanup"
            run_id = int(os.environ["OWNER_RUN_ID"] if external else os.environ["GITHUB_RUN_ID"])
            attempt = int(os.environ["OWNER_RUN_ATTEMPT"] if external else os.environ["GITHUB_RUN_ATTEMPT"])
            source = os.environ["SOURCE_SHA"]
            if args.command == "cleanup-target":
                output("tag", cleanup_target(api, run_id, attempt, source))
            else:
                cleanup(api, run_id, attempt, source, completed=external, tag=os.environ.get("RELEASE_TAG"))
            return 0
        flag = os.environ["RELEASE_PRERELEASE"]
        if flag not in ("true", "false"):
            raise ReleaseError("RELEASE_PRERELEASE must be true or false")
        build = Build(api.repository, os.environ["RELEASE_TAG"], os.environ["SOURCE_SHA"],
                      os.environ["ANDROID_IDENTITY"], int(os.environ["GITHUB_RUN_ID"]),
                      int(os.environ["GITHUB_RUN_ATTEMPT"]), flag == "true")
        if args.command == "prepare":
            release_id, public = prepare(api, args.assets, build, os.environ.get("RELEASE_BODY", ""))
            output("release_id", release_id)
            output("already_published", str(public).lower())
        else:
            release_id = os.environ["RELEASE_ID"]
            if not release_id.isdecimal() or int(release_id) <= 0:
                raise ReleaseError("Invalid release ID")
            (upload if args.command == "upload" else publish)(api, args.assets, build, release_id)
    except (ReleaseError, OSError, URLError, ValueError, KeyError, TypeError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
