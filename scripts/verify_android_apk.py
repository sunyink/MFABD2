"""Check an actual MaaFwApp APK contains the MFABD2 agent and ARM64 runtime."""

import io
import json
import argparse
import re
import sys
import zipfile
from pathlib import Path

import release_interface
from verify_branding import verify_archive


def verify_options(interface: dict) -> None:
    """Match the pinned Android UI's explicit option-type requirement.

    MaaFwApp discards options with an omitted/unknown type before resolving
    references. Merely checking that their names exist in JSON misses this.
    The rule itself lives with the rest of the release contract in release_interface.py.
    """
    release_interface.check_options(interface, android=True)


def verify_identity(metadata: dict, badging: str, signature: str) -> None:
    package = re.search(r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging, re.M)
    if not package or package.groups() != (
        metadata["application_id"], str(metadata["version_code"]), metadata["version_name"],
    ):
        raise ValueError("APK package/version does not match build metadata")
    if "application-debuggable" in badging:
        raise ValueError("Release APK must not be debuggable")
    # Two identities can sit on one phone, so their labels have to differ visibly —
    # otherwise the user is left picking between two identical-looking icons.
    label = re.search(r"^application-label:'([^']*)'", badging, re.M)
    if not label or label.group(1) != metadata["label"]:
        raise ValueError(f"APK label {label and label.group(1)!r} does not match metadata")
    certificates = [value.lower() for value in re.findall(r"certificate SHA-256 digest: ([0-9a-fA-F]+)", signature)]
    if certificates != [metadata["certificate_sha256"]]:
        raise ValueError("APK signer does not match the pinned certificate")


def verify(path: Path, metadata: dict | None = None, maafw: str | None = None) -> None:
    """maafw comes from scripts/detect_maa_version.py, never from a constant here."""
    with zipfile.ZipFile(path) as apk:
        names = set(apk.namelist())
        for name in ("lib/arm64-v8a/libMaaFramework.so", "lib/arm64-v8a/libMaaAgentServer.so"):
            content = apk.read(name)
            if content[:4] != b"\x7fELF" or int.from_bytes(content[18:20], "little") != 183:
                raise ValueError(f"Not an ARM64 native library: {name}")
        if any(name.startswith("lib/") and not name.startswith("lib/arm64-v8a/") for name in names):
            raise ValueError("This build must contain only ARM64 native libraries")
        descriptor = json.loads(apk.read("assets/agent/agent-runtime.json"))
        runtimes = descriptor["runtimes"]
        if len(runtimes) != 1 or runtimes[0]["args"] != ["-u", "agent/main.py"]:
            raise ValueError("Unexpected agent launch descriptor")
        with zipfile.ZipFile(io.BytesIO(apk.read("assets/pi.zip"))) as payload:
            verify_archive(payload)
            interface = json.loads(payload.read("interface.json"))
            # The same contract the build checked before packing: Adb only, base then the
            # native overlay, no RID, option types MaaFwApp parses, version, agent declared.
            target = release_interface.target_for(
                "android", metadata["version_name"] if metadata else None, announce=False)
            release_interface.verify(interface, target, on_disk=False)
            payload.getinfo("resource/android_native/pipeline/StartGame.json")
            payload.getinfo("agent/main.py")
            payload.getinfo("agent/utils/runtime_environment.py")
            if any(name.startswith("config/") or "agent_save_data" in name for name in payload.namelist()):
                raise ValueError("User config/save data must never be bundled")
        with zipfile.ZipFile(io.BytesIO(apk.read("assets/agent/bundle.zip"))) as bundle:
            bundle.getinfo("arm64-v8a/bin/python3")
            manifest = json.loads(bundle.read("arm64-v8a/agent-core.json"))
            if manifest["abi"] != "arm64-v8a":
                raise ValueError("Android agent core must be built for arm64-v8a")
            if maafw and manifest["provides"]["maafw"] != maafw:
                raise ValueError(
                    f"Agent core ships MaaFw {manifest['provides']['maafw']}, "
                    f"but the project resolved {maafw}"
                )
            for prefix in ("arm64-v8a/site-packages/PIL/_imaging", "arm64-v8a/site-packages/numpy/_core/_multiarray_umath"):
                if not any(name.startswith(prefix) and name.endswith(".so") for name in bundle.namelist()):
                    raise ValueError(f"Android native Python extension missing: {prefix}")
    print(f"APK structure verified (ARM64, PI, Python agent, NumPy, Pillow): {path}")


if __name__ == "__main__":
    # Slices, not indexes: a bare invocation — or a `--interface` with no path —
    # must reach argparse's usage message rather than dying on IndexError.
    if sys.argv[1:2] == ["--interface"] and len(sys.argv) >= 3:
        verify_options(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
        print("Android interface option checks passed")
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("apk", type=Path)
        parser.add_argument("--metadata", type=Path)
        parser.add_argument("--badging", type=Path)
        parser.add_argument("--signature", type=Path)
        parser.add_argument("--maafw", help="resolved framework version; omit to skip that check")
        args = parser.parse_args()
        metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else None
        if metadata:
            if not args.badging or not args.signature:
                parser.error("metadata requires aapt2 badging and apksigner output")
            verify_identity(metadata, args.badging.read_text(), args.signature.read_text())
        verify(args.apk, metadata, args.maafw)
