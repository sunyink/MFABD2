"""Single source of truth for the MaaFramework version every target ships.

The version is never written down anywhere: it is read out of the MFAAvalonia
binary pinned by requirements.txt, so desktop and Android cannot disagree.
Both install.yml and android.yml call this script.
"""

import argparse
import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The `#` must be immediately followed by the key: requirements.txt also carries a
# `例: # MFA_CORE_TAG=v5.12.2` documentation line that must never be picked up.
TAG_PATTERNS = {
    "mfaa_tag": re.compile(r"^#\s*MFAA_TAG=(v[\w.\-]+)"),
    "mfa_core_tag": re.compile(r"^#\s*MFA_CORE_TAG=(v[\w.\-]+)"),
}
# Upstream tags look like `3.13.15-maafw5.12.3` — CPython version, then ours.
AGENT_CORE_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)-maafw(.+)$")
# MaaFwApp pins its agent core as a module constant in the bundle builder.
CORE_TAG_PATTERN = re.compile(r"""^CORE_TAG\s*=\s*["']([^"']+)["']""", re.MULTILINE)


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


def agent_core_tag(maafw: str, upstream: Path) -> str:
    """读上游钉住的 agent core，并校验桌面内核跟得上——不替上游挑版本。

    MaaFwApp 的 Kotlin 侧、agent core 里的 CPython、以及 core 内嵌的框架是
    一个一起验过的组合。在这里另选一个"版本号看着对"的 tag，等于绕过上游的验证；
    所以这里只读它自己写下的常量，两边对不上就停，由人去调 requirements.txt
    或换 MaaFwApp 的 pin。
    """
    source = upstream / "scripts" / "build_agent_bundle.py"
    if not source.is_file():
        raise ValueError(f"找不到 {source}，确认 MaaFwApp 已 checkout 到 {upstream}")
    found = CORE_TAG_PATTERN.search(source.read_text(encoding="utf-8"))
    if not found:
        raise ValueError(f"{source} 里读不到 CORE_TAG 常量——上游换了写法，这里要跟着改")
    tag = found.group(1)
    match = AGENT_CORE_PATTERN.match(tag)
    if not match:
        raise ValueError(f"上游的 CORE_TAG={tag} 不是 <CPython版本>-maafw<框架版本> 格式")
    if (pinned := match.group(4)) != maafw:
        raise ValueError(
            f"框架版本对不上：上游 MaaFwApp 钉的 agent core 是 {tag}（框架 {pinned}），"
            f"而 requirements.txt 钉住的桌面内核是 {maafw}。安卓与桌面不能用不同版本的框架。"
            f"两条出路：把 requirements.txt 的 MFA_CORE_TAG 设成 v{pinned}，"
            f"或换一个 CORE_TAG 为 {maafw} 的 MaaFwApp commit。"
        )
    return tag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("parse", help="读 requirements.txt 里钉住的 tag")
    detect = commands.add_parser("detect", help="从 MFAAvalonia 的二进制里读出内核版本")
    detect.add_argument("--assets", type=Path, required=True, help="解压后的 MFAAvalonia 目录")
    detect.add_argument("--override", type=Path, help="急救内核源目录，存在时先原地覆盖")
    core = commands.add_parser("agent-core-tag", help="读上游钉住的 agent core 并校验版本一致")
    core.add_argument("--maafw", required=True)
    core.add_argument("--upstream", type=Path, default=ROOT / "android-upstream", help="MaaFwApp 的 checkout 目录")
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
            emit(agent_core_tag=agent_core_tag(args.maafw, args.upstream))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
