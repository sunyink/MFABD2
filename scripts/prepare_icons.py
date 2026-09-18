"""Optional release branding. Failed operations leave the original files usable."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
RCEDIT_URL = "https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe"
RCEDIT_SHA256 = "3e7801db1a5edbec91b49a24a094aad776cb4515488ea5a4ca2289c400eade2a"
PROFILE_ICON = re.compile(r"(?m)^  icon: [^\r\n]+(?=\r?$)")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_png(path: Path) -> bytes:
    """Validate our non-interlaced RGB/RGBA PNGs without a CI image dependency."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Invalid PNG: {path}")
    offset, compressed, header, ended = 8, bytearray(), None, False
    while offset < len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(kind + body) != crc:
            raise ValueError(f"PNG checksum mismatch: {path}")
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"IEND":
            ended = True
        offset += size + 12
    if not ended or not header:
        raise ValueError(f"Incomplete PNG: {path}")
    width, height, depth, color, compression, filtering, interlace = header
    if not (0 < width == height <= 1024 and depth == 8 and color in (2, 6)
            and compression == filtering == interlace == 0):
        raise ValueError(f"Unsupported branding PNG layout: {path}")
    stride = width * (4 if color == 6 else 3) + 1
    pixels = zlib.decompress(compressed)
    if len(pixels) != stride * height or any(pixels[i] > 4 for i in range(0, len(pixels), stride)):
        raise ValueError(f"Invalid PNG pixel data: {path}")
    return data


def apply_runtime(install: Path, branding: Path) -> None:
    png = validate_png(branding / "title.png")
    interface_path = install / "interface.json"
    interface = json.loads(interface_path.read_text(encoding="utf-8"))
    # Write the image first: no interface can point to a missing/partial new image.
    atomic_write(install / "resource/ui/title.png", png)
    interface["icon"] = "resource/ui/title.png"
    atomic_write(interface_path, (json.dumps(interface, ensure_ascii=False, indent=4) + "\n").encode())


def ico_frames(path: Path) -> list[bytes]:
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved or kind != 1 or count != 7:
        raise ValueError("Expected the approved seven-size Windows ICO")
    frames, sizes = [], set()
    for index in range(count):
        width, height, _, _, _, depth, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        width, height = width or 256, height or 256
        if width != height or depth != 32 or not length or offset < 6 + 16 * count or offset + length > len(data):
            raise ValueError("Invalid Windows ICO entry")
        sizes.add(width)
        frames.append(data[offset:offset + length])
    if sizes != {16, 24, 32, 48, 64, 128, 256}:
        raise ValueError("Windows ICO sizes changed")
    return frames


def verify_windows_resources(path: Path, frames: list[bytes]) -> None:
    """Read the actual PE icon group as data; do not execute the candidate."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_ssize_t)
    kernel.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, callback_type, ctypes.c_ssize_t]
    kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel.FindResourceW.restype = wintypes.HANDLE
    kernel.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.SizeofResource.restype = wintypes.DWORD
    kernel.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    kernel.LoadResource.restype = wintypes.HANDLE
    kernel.LockResource.argtypes = [wintypes.HANDLE]
    kernel.LockResource.restype = ctypes.c_void_p
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    module = kernel.LoadLibraryExW(str(path.resolve()), None, 2)  # LOAD_LIBRARY_AS_DATAFILE
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    groups = []

    def read_resource(name, kind):
        resource = kernel.FindResourceW(module, name, kind)
        if not resource:
            raise ctypes.WinError(ctypes.get_last_error())
        size = kernel.SizeofResource(module, resource)
        address = kernel.LockResource(kernel.LoadResource(module, resource))
        if not address:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(address, size)

    @callback_type
    def collect(handle, kind, name, parameter):
        # Named resource pointers are valid only for the duration of the callback.
        groups.append(name if name <= 65535 else ctypes.c_wchar_p(ctypes.wstring_at(name)))
        return True

    try:
        if not kernel.EnumResourceNamesW(module, 14, collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        for group_name in groups:
            group = read_resource(group_name, 14)
            count = struct.unpack_from("<H", group, 4)[0]
            if count != len(frames):
                continue
            actual = [read_resource(struct.unpack_from("<H", group, 6 + index * 14 + 12)[0], 3)
                      for index in range(count)]
            if actual == frames:
                return
        raise ValueError("EXE icon groups do not reference the approved seven images")
    finally:
        kernel.FreeLibrary(module)


def apply_windows(install: Path, branding: Path) -> None:
    executable = install / "MFAAvalonia.exe"
    if not executable.is_file():
        raise FileNotFoundError(executable)
    frames = ico_frames(branding / "app.ico")
    # Never edit the real executable in place, even when rcedit fails halfway.
    with tempfile.TemporaryDirectory(prefix="mfabd2-icon-") as temporary:
        stage = Path(temporary)
        tool = stage / "rcedit.exe"
        with urllib.request.urlopen(RCEDIT_URL, timeout=45) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != RCEDIT_SHA256:
            raise ValueError("rcedit SHA-256 mismatch")
        tool.write_bytes(content)
        icon = stage / "app.ico"
        shutil.copyfile(branding / "app.ico", icon)
        candidate = stage / executable.name
        shutil.copy2(executable, candidate)
        subprocess.run([str(tool), str(candidate), "--set-icon", str(icon)], check=True, timeout=60)
        patched = candidate.read_bytes()
        if not all(frame in patched for frame in frames):
            raise ValueError("Patched EXE is missing an approved icon frame")
        verify_windows_resources(candidate, frames)
        atomic_write(executable, patched)


def apply_android(profile: Path, work: Path, branding: Path) -> None:
    png = validate_png(branding / "android.png")
    original = profile.read_bytes()
    source = original.decode("utf-8")
    if len(PROFILE_ICON.findall(source)) != 1:
        raise ValueError("Expected exactly one app.icon in the Android profile")
    destination = work / "branding/launcher.png"
    # Backup is made after identity preparation; release/CI IDs remain unchanged.
    backup = work / "branding/profile-before-icons.yaml"
    if backup.exists():
        raise ValueError("Android icon preparation already ran; refusing to replace its backup")
    atomic_write(destination, png)
    atomic_write(backup, original)
    relative = Path(os.path.relpath(destination, profile.parent)).as_posix()
    updated = PROFILE_ICON.sub(lambda _: "  icon: " + json.dumps(relative), source)
    atomic_write(profile, updated.encode())


def restore_android(profile: Path, work: Path) -> bool:
    backup = work / "branding/profile-before-icons.yaml"
    if not backup.is_file():
        return False
    atomic_write(profile, backup.read_bytes())
    backup.unlink()
    return True


def report(name: str, error: Exception | None = None) -> None:
    message = f"{name}: applied" if error is None else f"{name}: kept default/previous icon ({error})"
    if error:
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning title=Optional application icon::{escaped}")
    else:
        print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"- {message.replace(chr(10), ' ')}\n")
        except OSError as exc:
            print(f"Could not write icon summary: {exc}")


def optional(name, action) -> None:
    try:
        action()
    except Exception as exc:
        report(name, exc)
    else:
        report(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("win", "macos", "linux", "android"))
    parser.add_argument("--branding", type=Path, default=ROOT / "release" / "branding")
    parser.add_argument("--install", type=Path, default=ROOT / "install")
    parser.add_argument("--profile", type=Path, default=ROOT / "android/pi-profile.yaml")
    parser.add_argument("--work", type=Path, default=ROOT / "android-build")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if args.restore:
        # This command controls a build retry: missing/failed rollback must be fatal.
        if args.platform != "android" or not restore_android(args.profile, args.work):
            raise SystemExit("No prepared Android icon to roll back")
        report("Android launcher", RuntimeError("build failed; restored original icon for one retry"))
    elif args.platform == "android":
        optional("Android launcher", lambda: apply_android(args.profile, args.work, args.branding))
    else:
        optional("MFAA runtime", lambda: apply_runtime(args.install, args.branding))
        if args.platform == "win":
            optional("Windows EXE", lambda: apply_windows(args.install, args.branding))


if __name__ == "__main__":
    main()
