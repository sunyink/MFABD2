"""Android identity/version configuration. Signing secrets never enter metadata."""

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def version_code(run_number: int, attempt: int, offset: int = 0) -> int:
    # All builds, including release requests, run in android.yml itself.
    if run_number < 1 or not 1 <= attempt < 100 or offset < 0:
        raise ValueError("Invalid Android build sequence")
    value = offset + run_number * 100 + attempt
    if value > 2_100_000_000:
        raise ValueError("Android versionCode exceeds its supported range")
    return value


def display_version(requested: str, tags: list[str], sha: str, date: str, message: str) -> str:
    if requested:
        if not VERSION.fullmatch(requested):
            raise ValueError("Android display version must use the project's version format")
        return requested
    formal = [tuple(map(int, tag[1:].split('.'))) for tag in tags
              if re.fullmatch(r"v\d+\.\d+\.\d+", tag)]
    major, minor, patch = max(formal, default=(0, 0, 0))
    last_line = message.rstrip().splitlines()[-1] if message.strip() else ""
    channel = "ci"
    if "[deploy-alpha]" in last_line:
        channel, patch = "alpha", patch + 2
    elif "[deploy-beta]" in last_line:
        channel, patch = "beta", patch + 1
    return f"v{major}.{minor}.{patch}-{channel}.{date}.{sha}"


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Pinned UI build hook changed: {old}")
    return text.replace(old, new, 1)


def prepare_ui(upstream: Path, settings: dict, code: int) -> None:
    prefix, suffix = settings["application_id"].rsplit('.', 1)
    if suffix != "mfabd2" or not re.fullmatch(r"[a-z][a-z0-9_.]*", prefix):
        raise ValueError("Application ID must match the mfabd2 profile")
    gradle = upstream / "build-logic/convention/src/main/kotlin/com/aliothmoon/maafw/gradle"
    app_path = gradle / "AndroidApplicationConventionPlugin.kt"
    app = app_path.read_text(encoding="utf-8")
    app = replace_once(app, 'private const val BASE_APPLICATION_ID = "com.aliothmoon.maafw"',
                       f'private const val BASE_APPLICATION_ID = "{prefix}"')
    app = replace_once(app, 'private val SHIPPED_ABIS = listOf("arm64-v8a", "x86_64")',
                       'private val SHIPPED_ABIS = listOf("arm64-v8a")')
    version_path = gradle / "GitVersion.kt"
    version = version_path.read_text(encoding="utf-8")
    marker = "internal fun Project.gitVersionCode(): Int {"
    if version.count(marker) != 1:
        raise ValueError("Pinned UI versionCode hook changed")
    start = version.index(marker)
    end = version.index("\n}\n", start) + len("\n}\n")
    version = version[:start] + marker + f"\n    return {code}\n}}\n" + version[end:]
    app_path.write_text(app, encoding="utf-8")
    version_path.write_text(version, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["metadata", "prepare"])
    parser.add_argument("--version", default="")
    parser.add_argument("--upstream", type=Path, default=ROOT / "android-upstream")
    parser.add_argument("--metadata", type=Path, default=ROOT / "android-build/build-metadata.json")
    args = parser.parse_args()
    settings = json.loads((ROOT / "android/release.json").read_text(encoding="utf-8"))
    if not isinstance(settings["label"], str) or any(c in settings["label"] for c in "\r\n"):
        raise ValueError("Android application label must be a single line")
    if args.command == "prepare":
        metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
        prepare_ui(args.upstream, settings, metadata["version_code"])
        return
    version = display_version(args.version, git("tag", "-l", "v*").splitlines(),
                              git("rev-parse", "--short", "HEAD"),
                              datetime.now(timezone.utc).strftime("%y%m%d"), git("log", "-1", "--format=%B"))
    metadata = {
        "application_id": settings["application_id"],
        "label": settings["label"],
        "certificate_sha256": settings["certificate_sha256"],
        "key_alias": settings["key_alias"],
        "version_name": version,
        "version_code": version_code(int(os.environ["GITHUB_RUN_NUMBER"]),
                                     int(os.environ["GITHUB_RUN_ATTEMPT"]), settings["version_code_offset"]),
        "source_sha": git("rev-parse", "HEAD"),
        "apk_name": f"MFABD2-{version}-android-arm64.apk",
    }
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in metadata.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
