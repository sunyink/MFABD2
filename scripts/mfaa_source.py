"""Resolve the optional six-character MFAA fork override for one CI run."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

UPSTREAM = "MaaXYZ/MFAAvalonia"
FORK = "sunyink/MFAAvalonia"
FORK_BRANCH = "mfabd2"
RIDS = ("win-x64", "win-arm64", "linux-x64", "linux-arm64", "osx-x64", "osx-arm64")
MANIFEST = "fork-release.json"
STAMP = "mfaa-build.json"
LEGACY_TAG = re.compile(r"(?P<base>v\d+\.\d+\.\d+)-mfbd2\.g(?P<short>[0-9a-f]{6})")
NUMBERED_TAG = re.compile(
    r"(?P<base>v\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)-MFABD2\.beta\.(?P<number>[1-9]\d*)")


def api(path):
    result = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def asset_bytes(asset_id):
    result = subprocess.run(["gh", "api", f"repos/{FORK}/releases/assets/{asset_id}",
                             "-H", "Accept: application/octet-stream"], check=True, capture_output=True)
    return result.stdout


def package_names(tag):
    return {f"MFAAvalonia-{tag}-{rid}" + (".zip" if rid.startswith("win-") else ".tar.gz") for rid in RIDS}


def validate_identity(stamp, release_tag, sha):
    if (type(stamp.get("schema")) is not int or stamp["schema"] not in (1, 2)
            or stamp.get("repository") != FORK or stamp.get("branch") != FORK_BRANCH
            or stamp.get("commit") != sha or stamp.get("short_commit") != sha[:6]):
        raise ValueError("自家 MFAA 发布清单与指定提交不一致")
    package_tag = stamp.get("tag", "")
    numbered = NUMBERED_TAG.fullmatch(release_tag)
    if stamp["schema"] == 1:
        legacy = LEGACY_TAG.fullmatch(package_tag)
        if not legacy or legacy["short"] != sha[:6]:
            raise ValueError("旧版 MFAA 包来源标记不符")
        if release_tag != package_tag:
            # A historical release may move to a numbered tag while its original
            # package and signed/digested manifest remain byte-for-byte intact.
            if not numbered or numbered["base"] != legacy["base"]:
                raise ValueError("旧版 MFAA 包与编号标签的基础版本不符")
            sequence = stamp.get("fork_sequence")
            if sequence is not None and (type(sequence) is not int or sequence != int(numbered["number"])):
                raise ValueError("旧版 MFAA 包与编号标签的自家序号不符")
    else:
        sequence = stamp.get("fork_sequence")
        if (not numbered or package_tag != release_tag or type(sequence) is not int
                or not 1 <= sequence <= 2147483647 or sequence != int(numbered["number"])
                or stamp.get("upstream_version") != numbered["base"]):
            raise ValueError("MFAA 编号、基础版本与包内来源标记不符")
    return package_tag


def validate_manifest(manifest, release, sha):
    package_tag = validate_identity(manifest, release["tag_name"], sha)
    packages = manifest.get("packages", [])
    expected_names = package_names(package_tag)
    if len(packages) != len(RIDS) or {p.get("name") for p in packages} != expected_names:
        raise ValueError("自家 MFAA 缺少完整的六平台产物清单")
    assets = release.get("assets", [])
    by_name = {asset["name"]: asset for asset in assets}
    if len(by_name) != len(assets) or set(by_name) != expected_names | {MANIFEST}:
        raise ValueError("自家 MFAA Release 附件缺失、重复或与清单不一致")
    for package in packages:
        asset = by_name[package["name"]]
        sha256 = package.get("sha256", "")
        if (not re.fullmatch(r"[0-9a-f]{64}", sha256) or package.get("size", 0) <= 0
                or asset.get("size") != package["size"] or asset.get("state") != "uploaded"
                or asset.get("digest") != f"sha256:{sha256}"):
            raise ValueError(f"自家 MFAA 附件校验不符：{package['name']}")


def resolve_source(upstream_tag, override, fetch=api, download=asset_bytes):
    short = override.strip().lower()
    if not short:
        return dict(repository=UPSTREAM, tag=upstream_tag, commit="")
    if not re.fullmatch(r"[0-9a-f]{6}", short):
        raise ValueError("自家 MFAA 提交号必须恰好为六位十六进制字符；留空则使用 requirements.txt")
    sha = fetch(f"repos/{FORK}/commits/{short}").get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not sha.startswith(short):
        raise ValueError("六位提交号未能解析到唯一的完整提交")
    candidates = []
    page = 1
    while True:
        releases = fetch(f"repos/{FORK}/releases?per_page=100&page={page}")
        for release in releases:
            tag = release.get("tag_name", "")
            legacy = LEGACY_TAG.fullmatch(tag)
            numbered = NUMBERED_TAG.fullmatch(tag)
            if not release.get("draft") and (
                    legacy is not None and legacy["short"] == short
                    or numbered is not None and release.get("target_commitish") == sha):
                candidates.append(release)
        if len(releases) < 100:
            break
        page += 1
    if len(candidates) != 1:
        raise ValueError(f"提交 {short} 需要唯一的自家预发布包，当前找到 {len(candidates)} 个；请先在 mfabd2 分支完成构建")
    release = candidates[0]
    if release.get("prerelease") is not True:
        raise ValueError("自家 MFAA 只允许预发布包")
    tag = release["tag_name"]
    if fetch(f"repos/{FORK}/commits/{quote(tag, safe='')}").get("sha") != sha:
        raise ValueError("自家 MFAA 标签指向另一提交")
    manifests = [asset for asset in release.get("assets", []) if asset.get("name") == MANIFEST]
    if len(manifests) != 1 or not 0 < manifests[0].get("size", 0) <= 65536:
        raise ValueError("自家 MFAA 发布缺少有效构建清单")
    asset = manifests[0]
    raw = download(asset["id"])
    if (len(raw) != asset["size"] or asset.get("state") != "uploaded"
            or asset.get("digest") != f"sha256:{hashlib.sha256(raw).hexdigest()}"):
        raise ValueError("自家 MFAA 发布清单校验失败")
    validate_manifest(json.loads(raw), release, sha)
    return dict(repository=FORK, tag=tag, commit=sha)


def verify_extracted(root, tag, sha, rid):
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or rid not in RIDS:
        raise ValueError("自家 MFAA 校验需要完整提交与有效平台")
    stamps = list(root.rglob(STAMP))
    if len(stamps) != 1:
        raise ValueError("下载包中没有唯一的 MFAA 构建来源标记")
    stamp = json.loads(stamps[0].read_text(encoding="utf-8"))
    validate_identity(stamp, tag, sha)
    if stamp.get("rid") != rid:
        raise ValueError("下载包的平台或源码提交不符")
    print(f"Verified MFAA {tag}, {rid}, commit {sha}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--tag", required=True)
    resolve.add_argument("--commit", default="")
    verify = sub.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--rid", choices=RIDS, required=True)
    args = parser.parse_args()
    if args.action == "verify":
        verify_extracted(args.root, args.tag, args.commit, args.rid)
        return
    source = resolve_source(args.tag, args.commit)
    print(json.dumps(source, ensure_ascii=False, indent=2))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in source.items():
                stream.write(f"{key}={value}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"### MFAAvalonia package source\n\nRepository: `{source['repository']}`\n\n"
                         f"Tag: `{source['tag']}`\n\nCommit: `{source['commit'] or 'upstream requirements.txt pin'}`\n")


if __name__ == "__main__":
    main()
