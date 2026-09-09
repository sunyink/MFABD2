"""Host-owned Android runtime and save paths; desktop defaults remain unchanged."""

import os
import platform
import sys
from pathlib import Path


def is_android() -> bool:
    return (
        sys.platform == "android"
        or hasattr(sys, "getandroidapilevel")
        or platform.system().lower() == "android"
        or os.environ.get("PI_CLIENT_NAME") == "MaaFwApp"
        or os.environ.get("MFA_ANDROID_OUTPUT_BRIDGED") == "1"
    )


def android_library_dir() -> Path:
    """Never replace an Android host's native libraries with desktop runtimes."""
    value = os.environ.get("MAAFW_BINARY_PATH") or os.environ.get("MAA_LIBRARY_DIR")
    if not value:
        raise RuntimeError("Android host must provide MAAFW_BINARY_PATH or MAA_LIBRARY_DIR")
    directory = Path(value)
    if not directory.is_absolute():
        raise RuntimeError("Android native library directory must be absolute")
    for name in ("libMaaFramework.so", "libMaaAgentServer.so"):
        if not (directory / name).is_file():
            raise RuntimeError(f"Android native library is missing: {directory / name}")
    os.environ["MAAFW_BINARY_PATH"] = str(directory)
    return directory


def persistent_data_dir(project_root: Path) -> Path | None:
    """Return an explicit save directory, or None for legacy desktop selection.

    MaaFwApp replaces the entire files/pi tree on upgrade. MFAAvalonia preserves
    config while replacing payload-owned entries. Do not guess HOME on Android.
    """
    override = os.environ.get("MFABD2_DATA_DIR", "").strip()
    if override:
        directory = Path(override)
        if not directory.is_absolute():
            raise RuntimeError("MFABD2_DATA_DIR must be an absolute path")
        return directory.resolve()
    if not is_android():
        return None
    root = project_root.resolve()
    if os.environ.get("PI_CLIENT_NAME") == "MaaFwApp" and root.name == "pi":
        return root.parent / "mfabd2-save"
    if os.environ.get("MFA_ANDROID_OUTPUT_BRIDGED") == "1":
        return root / "config" / "MFABD2"
    raise RuntimeError("Unknown Android host: configure a persistent MFABD2_DATA_DIR")
