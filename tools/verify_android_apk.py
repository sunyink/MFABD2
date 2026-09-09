"""Check an actual MaaFwApp APK contains the MFABD2 agent and ARM64 runtime."""

import io
import json
import sys
import zipfile
from pathlib import Path


def verify_options(interface: dict) -> None:
    """Match the pinned Android UI's explicit option-type requirement.

    MaaFwApp discards options with an omitted/unknown type before resolving
    references. Merely checking that their names exist in JSON misses this.
    """
    options = interface.get("option", {})
    invalid = [name for name, value in options.items()
               if value.get("type") not in {"select", "switch", "checkbox", "input"}]
    if invalid:
        raise ValueError(f"Android UI cannot parse option types: {invalid}")
    references = [("global_option", interface.get("global_option", []))]
    for section in ("task", "resource", "controller"):
        for item in interface.get(section, []):
            references.append((f"{section}:{item.get('name')}", item.get("option", [])))
    for name, option in options.items():
        for case in option.get("cases", []):
            references.append((f"option:{name}/{case.get('name')}", case.get("option", [])))
    missing = [(owner, name) for owner, names in references for name in names if name not in options]
    if missing:
        raise ValueError(f"Android option references are missing: {missing}")


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
            verify_options(interface)
            controllers = interface.get("controller", [])
            if len(controllers) != 1 or controllers[0].get("type") != "Adb":
                raise ValueError("Android payload must expose only its Adb controller declaration")
            resources = interface.get("resource", [])
            if not resources or any(
                resource.get("controller") and controllers[0]["name"] not in resource["controller"]
                for resource in resources
            ):
                raise ValueError("Android payload contains incompatible resource choices")
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
    if sys.argv[1] == "--interface":
        verify_options(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
        print("Android interface option checks passed")
    else:
        verify(Path(sys.argv[1]))
