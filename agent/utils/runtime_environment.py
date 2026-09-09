"""Resolve platform policy once at startup; consumers use the resulting config."""

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoragePolicy:
    directory: Path
    # None forbids fallback; a desktop portable archive takes precedence.
    portable_root: Path | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    mode: str
    library_dir: Path | None
    storage: StoragePolicy
    manage_venv: bool
    prepend_library_to_path: bool
    strict_storage: bool

    @classmethod
    def detect(cls, project_root: Path, *, enable_venv_auto_check: bool = True):
        root = project_root.resolve()
        system = platform.system().lower()
        android = _is_android(system)
        if android:
            mode = "android"
            library_dir = _android_library_dir()
        elif (root / "requirements.txt").exists():
            mode = "dev"
            library_dir = None
        else:
            mode = "release"
            os_name = {"windows": "win", "linux": "linux", "darwin": "osx"}.get(system)
            if os_name is None:
                raise RuntimeError(f"Unsupported release platform: {system}")
            arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
            library_dir = root / "runtimes" / f"{os_name}-{arch}" / "native"

        embedded = system == "windows" and root in Path(sys.executable).resolve().parents
        return cls(
            project_root=root,
            mode=mode,
            library_dir=library_dir,
            storage=resolve_storage_policy(root, system=system, android=android),
            manage_venv=mode == "dev" and enable_venv_auto_check and not embedded,
            prepend_library_to_path=mode == "release" and system == "windows",
            strict_storage=android,
        )

    def prepare(self) -> None:
        """Apply the interpreter/library policy before importing maa."""
        from . import mfaalog

        if self.manage_venv:
            from . import venv_ops
            mfaalog.info("开发模式: 启动虚拟环境管理...")
            venv_ops.ensure_venv(self.project_root)

        if self.library_dir is not None:
            if self.mode == "android":
                for name in ("libMaaFramework.so", "libMaaAgentServer.so"):
                    if not (self.library_dir / name).is_file():
                        raise RuntimeError(f"Android native library is missing: {self.library_dir / name}")
            os.environ["MAAFW_BINARY_PATH"] = str(self.library_dir)
            if self.prepend_library_to_path:
                os.environ["PATH"] = str(self.library_dir) + os.pathsep + os.environ.get("PATH", "")
            mfaalog.info(f"运行模式: {self.mode} | 内核库: {self.library_dir}")
        else:
            mfaalog.info("开发模式: 使用 Python 环境自带内核库")


def _is_android(system: str) -> bool:
    return (
        sys.platform == "android"
        or hasattr(sys, "getandroidapilevel")
        or system == "android"
        or os.environ.get("PI_CLIENT_NAME") == "MaaFwApp"
        or os.environ.get("MFA_ANDROID_OUTPUT_BRIDGED") == "1"
    )


def _android_library_dir() -> Path:
    value = os.environ.get("MAAFW_BINARY_PATH") or os.environ.get("MAA_LIBRARY_DIR")
    if not value:
        raise RuntimeError("Android host must provide MAAFW_BINARY_PATH or MAA_LIBRARY_DIR")
    directory = Path(value)
    if not directory.is_absolute():
        raise RuntimeError("Android native library directory must be absolute")
    return directory


def resolve_storage_policy(
    project_root: Path, *, system: str | None = None, android: bool | None = None,
) -> StoragePolicy:
    """Also used once by standalone store callers which do not run main.py."""
    root = project_root.resolve()
    system = system if system is not None else platform.system().lower()
    android = _is_android(system) if android is None else android
    override = os.environ.get("MFABD2_DATA_DIR", "").strip()
    if override:
        directory = Path(override)
        if not directory.is_absolute():
            raise RuntimeError("MFABD2_DATA_DIR must be an absolute path")
        return StoragePolicy(directory.resolve())
    if android:
        if os.environ.get("PI_CLIENT_NAME") == "MaaFwApp" and root.name == "pi":
            return StoragePolicy(root.parent / "mfabd2-save")
        if os.environ.get("MFA_ANDROID_OUTPUT_BRIDGED") == "1":
            return StoragePolicy(root / "config" / "MFABD2")
        raise RuntimeError("Unknown Android host: configure a persistent MFABD2_DATA_DIR")
    if system == "windows":
        base = os.getenv("APPDATA") or os.path.expanduser("~")
    elif system == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return StoragePolicy(Path(base) / "MFABD2", portable_root=root)
