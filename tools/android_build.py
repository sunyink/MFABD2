"""Android identity/version configuration. Signing secrets never enter metadata."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
MAX_VERSION_CODE = 2_100_000_000


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def version_code(run_number: int, attempt: int, offset: int = 0) -> int:
    # One workflow counter for ALL channels. Re-running an older run keeps its slot;
    # dispatch a new run when reissuing old source as a newer installable build.
    if (any(type(value) is not int for value in (run_number, attempt, offset))
            or run_number < 1 or not 1 <= attempt < 100 or offset < 0):
        raise ValueError("Invalid Android build sequence")
    value = offset + run_number * 100 + attempt
    if value > MAX_VERSION_CODE:
        raise ValueError("Android versionCode exceeds its supported range")
    return value


def apk_name(version: str, code: int) -> str:
    if not VERSION.fullmatch(version) or type(code) is not int or not 1 <= code <= MAX_VERSION_CODE:
        raise ValueError("Invalid Android asset version")
    return f"MFABD2-{version}-android-arm64-vc{code}.apk"


def finalize_artifacts(apk: Path, metadata: dict, output: Path) -> None:
    """Called after APK identity/content verification; publish a matching triplet."""
    name = apk_name(metadata["version_name"], metadata["version_code"])
    if metadata["apk_name"] != name:
        raise ValueError("APK asset name does not match its version metadata")
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(apk, output / name)
    manifest = {**metadata, "schema_version": 1, "apk_sha256": digest}
    (output / f"{name}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")


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
    # Keep MFABD2's update policy separate from the pinned upstream implementation.
    module = upstream / "app/src/main/java/com/aliothmoon/maafw/di/UpdateModule.kt"
    source = module.read_text(encoding="utf-8")
    source = replace_once(source, "import com.aliothmoon.maafw.update.GitHubUpdateClient",
                          "import com.aliothmoon.maafw.update.Mfabd2GitHubUpdateClient")
    source = replace_once(source, "single { GitHubUpdateClient(get()) }",
                          "single { Mfabd2GitHubUpdateClient(get(), get()) }")
    source = replace_once(source, "get<GitHubUpdateClient>()", "get<Mfabd2GitHubUpdateClient>()")
    module.write_text(source, encoding="utf-8")
    preferences = upstream / "app/src/main/java/com/aliothmoon/maafw/settings/AppSettings.kt"
    source = preferences.read_text(encoding="utf-8")
    source = replace_once(source, '@PrefKey(default = "MIRRORCHYAN")\n    val updateSource: String = "MIRRORCHYAN"',
                          '@PrefKey(default = "GITHUB")\n    val updateSource: String = "GITHUB"')
    preferences.write_text(source, encoding="utf-8")
    for kind in ("main", "test"):
        destination = upstream / f"app/src/{kind}/java/com/aliothmoon/maafw/update"
        destination.mkdir(parents=True, exist_ok=True)
        for template in (ROOT / "android/update" / kind).glob("*.kt"):
            content = template.read_text(encoding="utf-8").replace(
                "@CERTIFICATE_SHA256@", settings["certificate_sha256"])
            (destination / template.name).write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["metadata", "prepare", "artifacts"])
    parser.add_argument("--version", default="")
    parser.add_argument("--upstream", type=Path, default=ROOT / "android-upstream")
    parser.add_argument("--metadata", type=Path, default=ROOT / "android-build/build-metadata.json")
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "android-build/artifacts")
    args = parser.parse_args()
    settings = json.loads((ROOT / "android/release.json").read_text(encoding="utf-8"))
    if not isinstance(settings["label"], str) or any(c in settings["label"] for c in "\r\n"):
        raise ValueError("Android application label must be a single line")
    if args.command in ("prepare", "artifacts"):
        metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
        if args.command == "prepare":
            prepare_ui(args.upstream, settings, metadata["version_code"])
        else:
            if not args.apk:
                parser.error("artifacts requires --apk")
            finalize_artifacts(args.apk, metadata, args.output)
        return
    version = display_version(args.version, git("tag", "-l", "v*").splitlines(),
                              git("rev-parse", "--short", "HEAD"),
                              datetime.now(timezone.utc).strftime("%y%m%d"), git("log", "-1", "--format=%B"))
    code = version_code(int(os.environ["GITHUB_RUN_NUMBER"]),
                        int(os.environ["GITHUB_RUN_ATTEMPT"]), settings["version_code_offset"])
    metadata = {
        "application_id": settings["application_id"],
        "label": settings["label"],
        "certificate_sha256": settings["certificate_sha256"],
        "key_alias": settings["key_alias"],
        "version_name": version,
        "version_code": code,
        "source_sha": git("rev-parse", "HEAD"),
        "apk_name": apk_name(version, code),
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
