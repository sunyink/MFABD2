"""Single source of truth for the MaaFramework version every target ships.

The version is never written down anywhere: it is read out of the MFAAvalonia
binary pinned by requirements.txt, so desktop and Android cannot disagree.
Both install.yml and android.yml call this script.
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_CORE_REPO = "Aliothmoon/MaaAgentCoreAndroid"

# The `#` must be immediately followed by the key: requirements.txt also carries a
# `例: # MFA_CORE_TAG=v5.12.2` documentation line that must never be picked up.
TAG_PATTERNS = {
    "mfaa_tag": re.compile(r"^#\s*MFAA_TAG=(v[\w.\-]+)"),
    "mfa_core_tag": re.compile(r"^#\s*MFA_CORE_TAG=(v[\w.\-]+)"),
}
# Upstream tags look like `3.13.15-maafw5.12.3` — CPython version, then ours.
AGENT_CORE_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)-maafw(.+)$")


def emit(**values: str) -> None:
    """Print for humans, append to GITHUB_OUTPUT when a workflow is listening."""
    for key, value in values.items():
        print(f"{key}={value}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")


def parse_tags(path: Path) -> dict[str, str]:
    found = {key: "" for key in TAG_PATTERNS}
    for line in path.read_text(encoding="utf-8").splitlines():
        for key, pattern in TAG_PATTERNS.items():
            if match := pattern.match(line.strip()):
                found[key] = match.group(1)
    if not found["mfaa_tag"]:
        raise ValueError(f"{path} 缺少 '# MFAA_TAG=' 配置行")
    return found


def find_library(assets: Path) -> Path:
    # symbols/ holds debug companions of the same name; loading one of those would
    # either fail or report a version that never ships.
    candidates = sorted(p for p in assets.rglob("libMaaFramework.so") if "symbols" not in p.parts)
    if not candidates:
        raise ValueError(f"{assets} 下找不到 libMaaFramework.so")
    # Absolute, so dlopen resolves it as a path rather than a search-path lookup.
    return candidates[0].resolve()


def dependency_report(library: Path) -> str:
    """ldd is the fastest way to see which dependency went missing."""
    try:
        result = subprocess.run(["ldd", str(library)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"（ldd 不可用：{error}）"
    return f"ldd 输出：\n{result.stdout}{result.stderr}"


def overlay_core(source: Path, target: Path) -> None:
    script = ROOT / "scripts" / "overlay_core.sh"
    script.chmod(script.stat().st_mode | 0o755)
    subprocess.run(["bash", str(script), str(source), str(target)], check=True)


def ensure_library_path(library: Path) -> None:
    """Re-exec with LD_LIBRARY_PATH set, because setting it in-process is too late.

    glibc reads the variable once when a process starts; assigning os.environ here
    would not affect our own dlopen. Re-executing is what makes callers able to run
    this script as a plain one-liner instead of exporting the path themselves.
    Only the Linux runners reach this — a .so does not load anywhere else.
    """
    directory = str(library.parent)
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    if directory in existing.split(os.pathsep):
        return
    environment = {**os.environ, "LD_LIBRARY_PATH": os.pathsep.join(filter(None, [directory, existing]))}
    os.execve(sys.executable, [sys.executable, *sys.argv], environment)


def read_version(library: Path) -> str:
    try:
        handle = ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
    except OSError as error:
        raise ValueError(f"加载 {library} 失败：{error}\n{dependency_report(library)}") from error
    handle.MaaVersion.restype = ctypes.c_char_p
    raw = handle.MaaVersion()
    if not raw:
        raise ValueError(f"{library} 的 MaaVersion() 没有返回版本")
    return raw.decode("utf-8").strip()


def github_releases(repo: str) -> list[dict]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases?per_page=100",
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    if token := (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def agent_core_tag(maafw: str) -> str:
    """Only the framework half of the tag is ours; the CPython half must be looked up."""
    matches = []
    for release in github_releases(AGENT_CORE_REPO):
        if release.get("draft"):
            continue
        match = AGENT_CORE_PATTERN.match(release.get("tag_name", ""))
        if match and match.group(4) == maafw:
            matches.append((tuple(int(match.group(i)) for i in (1, 2, 3)), release["tag_name"]))
    if not matches:
        raise ValueError(
            f"{AGENT_CORE_REPO} 尚未发布匹配 MaaFramework {maafw} 的 agent core。"
            "安卓不能使用与桌面不同版本的框架，构建到此为止。"
        )
    return max(matches)[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("parse", help="读 requirements.txt 里钉住的 tag")
    detect = commands.add_parser("detect", help="从 MFAAvalonia 的二进制里读出内核版本")
    detect.add_argument("--assets", type=Path, required=True, help="解压后的 MFAAvalonia 目录")
    detect.add_argument("--override", type=Path, help="急救内核源目录，存在时先原地覆盖")
    core = commands.add_parser("agent-core-tag", help="查与该内核版本匹配的安卓 agent core")
    core.add_argument("--maafw", required=True)
    args = parser.parse_args()

    try:
        if args.command == "parse":
            emit(**parse_tags(ROOT / "requirements.txt"))
        elif args.command == "detect":
            library = find_library(args.assets)
            # Before any real work, so the re-exec does not repeat the overlay.
            ensure_library_path(library)
            # Overlay first, load second: the path stays put while the bytes change,
            # so the version reported is the one that actually ships.
            if args.override and args.override.is_dir():
                overlay_core(args.override, args.assets)
            raw = read_version(library)
            version = raw[1:] if raw.startswith("v") else raw
            emit(version=version, tag=f"v{version}")
        else:
            emit(agent_core_tag=agent_core_tag(args.maafw))
    except (ValueError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
