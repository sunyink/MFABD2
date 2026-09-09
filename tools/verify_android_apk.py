"""Check an actual MaaFwApp APK contains the MFABD2 agent and ARM64 runtime."""

import io
import json
import sys
import zipfile
from pathlib import Path


def verify(path: Path) -> None:
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
            interface = json.loads(payload.read("interface.json"))
            if not interface.get("agent"):
                raise ValueError("PI does not declare its required agent")
            payload.getinfo("agent/main.py")
            payload.getinfo("agent/utils/runtime_environment.py")
            if any(name.startswith("config/") or "agent_save_data" in name for name in payload.namelist()):
                raise ValueError("User config/save data must never be bundled")
        with zipfile.ZipFile(io.BytesIO(apk.read("assets/agent/bundle.zip"))) as bundle:
            bundle.getinfo("arm64-v8a/bin/python3")
            manifest = json.loads(bundle.read("arm64-v8a/agent-core.json"))
            if manifest["provides"]["maafw"] != "5.12.3" or manifest["abi"] != "arm64-v8a":
                raise ValueError("Python binding and native core must match the pinned version")
            for prefix in ("arm64-v8a/site-packages/PIL/_imaging", "arm64-v8a/site-packages/numpy/_core/_multiarray_umath"):
                if not any(name.startswith(prefix) and name.endswith(".so") for name in bundle.namelist()):
                    raise ValueError(f"Android native Python extension missing: {prefix}")
    print(f"APK structure verified (ARM64, PI, Python agent, NumPy, Pillow): {path}")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
