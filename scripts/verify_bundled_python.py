"""Load the dependencies with the interpreter bundled into a desktop package.

Packaging only resolves wheels (Windows even cross-installs them with --platform),
so nothing proves they import until the bundled interpreter tries. Run it the way
the release launches the agent:

    install/python/python.exe -B -s scripts/verify_bundled_python.py --python 3.11 \
        --library-dir install/runtimes/win-x64/native
"""

import argparse
import importlib
import importlib.metadata
import os
import platform
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Native extensions or native libraries: a successful pip resolve says nothing about these.
IMPORTS = ["numpy", "PIL.Image", "maa", "loguru", "jsonc", "requests", "pytz"]
PIN = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([^\s;#]+)")


def pinned(requirements: Path) -> dict:
    pins = {}
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line)
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def check(expected_python: str, requirements: Path, library_dir) -> list:
    errors = []
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual != expected_python:
        errors.append(f"bundled Python is {platform.python_version()}, expected {expected_python}")

    for name, wanted in pinned(requirements).items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        if installed != wanted:
            errors.append(f"{name} is {installed or 'missing'}, requirements.txt pins {wanted}")

    if library_dir is not None:
        if not library_dir.is_dir():
            errors.append(f"native library directory is missing: {library_dir}")
            return errors
        # Same policy as RuntimeConfig.prepare in release mode.
        os.environ["MAAFW_BINARY_PATH"] = str(library_dir.resolve())
        if sys.platform == "win32":
            os.environ["PATH"] = str(library_dir.resolve()) + os.pathsep + os.environ.get("PATH", "")

    for module in IMPORTS:
        try:
            importlib.import_module(module)
        except Exception as error:  # report every failure, not just the first
            errors.append(f"import {module} failed: {error!r}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", required=True, help="expected major.minor, e.g. 3.11")
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    parser.add_argument("--library-dir", type=Path, help="runtimes/<rid>/native the release points maa at")
    args = parser.parse_args(argv)

    errors = check(args.python, args.requirements, args.library_dir)
    print(f"Python {platform.python_version()} at {sys.executable}")
    for name in ("numpy", "Pillow", "maafw"):
        try:
            print(f"  {name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            print(f"  {name} missing")
    for error in errors:
        print(f"::error::{error}")
    if errors:
        return 1
    print("Bundled Python imports every dependency")
    return 0


if __name__ == "__main__":
    sys.exit(main())
