"""Read the current user's official PC installations without launching anything."""

import ntpath
import os
import re
import stat
from dataclasses import dataclass

from .common import PreparationError

INSTALL_KEY = r"Software\Neowiz\Browndust2Starter"
GAME_EXE = "browndust ii.exe"


@dataclass(frozen=True)
class Installation:
    number: str
    directory: str
    executable: str

    @property
    def path(self):
        return ntpath.join(self.directory, self.executable)


@dataclass(frozen=True)
class Discovery:
    candidates: tuple
    diagnostics: tuple


@dataclass(frozen=True)
class Selection:
    installation: Installation
    uri: str
    diagnostics: tuple


class _InvalidRecord(ValueError):
    pass


def _absolute_directory(value):
    # Before Python 3.13, isabs accepts a root without a drive (\game). Do not let the
    # pretask working directory or current drive complete an installation path.
    path = value.replace("/", "\\")
    if path.lower().startswith("\\\\?\\unc\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    drive, tail = ntpath.splitdrive(path)
    if re.fullmatch(r"[a-zA-Z]:", drive):
        return tail.startswith("\\")
    if drive.startswith("\\\\"):
        parts = drive[2:].split("\\")
        return (len(parts) == 2 and all(part and part not in (".", "?", "..") for part in parts)
                and (not tail or tail.startswith("\\")))
    return False


def file_issue(path, *, stat_file=None):
    """Return a definite invalid-file reason; let unknown/denied access fail."""
    try:
        metadata = (stat_file or os.stat)(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        # Windows also maps an unreachable network path/name (53/67) to
        # FileNotFoundError. Those do not prove that an installation is stale.
        if getattr(exc, "winerror", None) not in (None, 2, 3, 267):
            raise
        return "游戏文件不存在"
    except ValueError:
        return "游戏文件路径无效"
    return None if stat.S_ISREG(metadata.st_mode) else "游戏路径不是普通文件"


def _text_value(registry, key, name, check, *, required=True):
    check()
    try:
        value, kind = registry.QueryValueEx(key, name)
    except FileNotFoundError:
        if required:
            raise _InvalidRecord(f"缺少 {name}")
        return None
    check()
    if kind not in (registry.REG_SZ, registry.REG_EXPAND_SZ) or not isinstance(value, str):
        if required:
            raise _InvalidRecord(f"{name} 不是字符串类型")
        return None
    if kind == registry.REG_EXPAND_SZ:
        value = registry.ExpandEnvironmentStrings(value)
        check()
    if not value or "\x00" in value:
        if required:
            raise _InvalidRecord(f"{name} 为空或含空字符")
        return None
    return value


def _failure(message, diagnostics, exc):
    detail = str(exc) or type(exc).__name__
    return PreparationError("；".join([message, *diagnostics, detail]))


def discover_installations(*, check, registry=None, stat_file=None):
    """Read a complete single-view snapshot; partial discovery is never unique."""
    if registry is None:
        import winreg as registry

    candidates, diagnostics = [], []
    check()
    try:
        root = registry.OpenKey(registry.HKEY_CURRENT_USER, INSTALL_KEY, 0,
                                registry.KEY_ENUMERATE_SUB_KEYS)
    except FileNotFoundError:
        check()
        return Discovery((), ("当前 Windows 用户下没有 Browndust2Starter 安装记录",))
    except OSError as exc:
        raise _failure("无法读取当前 Windows 用户的安装记录", diagnostics, exc) from exc

    phase = "枚举安装子项"
    try:
        with root:
            index = 0
            while True:
                check()
                phase = "枚举安装子项"
                try:
                    number = registry.EnumKey(root, index)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 259:
                        break
                    raise
                index += 1
                check()
                if not re.fullmatch(r"[0-9]+", number):
                    diagnostics.append(f"排除安装 {number!r}：编号不是半角数字")
                    continue
                try:
                    phase = f"读取安装 {number}"
                    with registry.OpenKey(root, number, 0, registry.KEY_QUERY_VALUE) as key:
                        directory = _text_value(registry, key, "path", check)
                        executable = _text_value(registry, key, "execute", check)
                        if not _absolute_directory(directory):
                            raise _InvalidRecord(f"path 不是完整绝对目录：{directory!r}")
                        if executable.lower() != GAME_EXE:
                            raise _InvalidRecord(f"execute 不是游戏文件名：{executable!r}")
                        item = Installation(number, directory, executable)
                        phase = f"查询游戏文件 {item.path!r}"
                        check()
                        issue = file_issue(item.path, stat_file=stat_file)
                        check()
                        if issue:
                            raise _InvalidRecord(f"{issue}：{item.path!r}")
                        phase = f"读取安装 {number} 的诊断字段"
                        version = _text_value(registry, key, "version", check, required=False)
                        environment = _text_value(registry, key, "environment", check, required=False)
                        candidates.append(item)
                        diagnostics.append(
                            f"有效安装 {number}：{item.path!r}（文件存在；"
                            f"version={version!r}, environment={environment!r}）")
                except _InvalidRecord as exc:
                    diagnostics.append(f"排除安装 {number}：{exc}")
    except OSError as exc:
        raise _failure(f"安装记录读取不完整（{phase}），不自动选择", diagnostics, exc) from exc
    check()
    return Discovery(tuple(candidates), tuple(diagnostics))


def select_installation(discovery):
    if len(discovery.candidates) != 1:
        reason = ("当前 Windows 用户下未找到有效安装" if not discovery.candidates
                  else "当前 Windows 用户下存在多个有效安装，无法唯一选择；请先手动打开目标游戏再连接")
        raise PreparationError("；".join([reason, *discovery.diagnostics]))
    item = discovery.candidates[0]
    uri = f"browndust2:games/{item.number}?usn=0"
    diagnostics = discovery.diagnostics + (
        f"选择唯一有效安装 {item.number}：{item.path!r}；启动地址 {uri}",)
    return Selection(item, uri, diagnostics)
