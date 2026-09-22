"""Isolated Android bootstrap probe and independent real MaaFw import check.

The simulated probe never calls main(). The real probe registers actual Python
callbacks with the host MaaFw library, but never connects a controller or host.
"""

import argparse
import dataclasses
import importlib
import importlib.abc
import inspect
import json
import os
import platform
import runpy
import sys
import tempfile
from contextlib import ExitStack
from importlib.metadata import distribution, version
from pathlib import Path
from unittest.mock import patch

from android_test_support import ROOT, EXPORTS, forbidden, install_maa_stub, verify_business_sources


def check_log_events():
    from maa.event_sink import NotificationType
    from utils import mfaalog
    from utils.log_event_sink import LogEventSink

    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "maa_option.json"
        sink = LogEventSink(config)
        for option in ("recording", "save_draw", "show_hit_box"):
            config.write_text(json.dumps({option: True}), encoding="utf-8")
            mfaalog.set_debug_ui_enabled(False)
            sink.on_tasker_task(None, NotificationType.Starting, {})
            assert mfaalog._debug_ui_enabled, option
        # Configuration is read on Starting only, not polled on other events.
        config.write_text("{}", encoding="utf-8")
        for event in (NotificationType.Unknown, NotificationType.Succeeded, NotificationType.Failed):
            sink.on_tasker_task(None, event, {})
            assert mfaalog._debug_ui_enabled, event
        sink.on_tasker_task(None, NotificationType.Starting, {})
        assert not mfaalog._debug_ui_enabled
        for text in ('{"recording": "true", "save_draw": 1}', '{"save_on_error": true}', "[]", "{"):
            config.write_text(text, encoding="utf-8")
            mfaalog.set_debug_ui_enabled(True)
            sink.on_tasker_task(None, NotificationType.Starting, {})
            assert not mfaalog._debug_ui_enabled, text
        config.unlink()
        mfaalog.set_debug_ui_enabled(True)
        sink.on_tasker_task(None, NotificationType.Starting, {})
        assert not mfaalog._debug_ui_enabled


class BlockBusinessImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "utils.log_event_sink":
            raise ImportError("Injected business import failure")
        return None


def bootstrap(fault):
    modules = install_maa_stub()
    server = modules["maa.agent.agent_server"].AgentServer
    if fault == "missing-event-module":
        del sys.modules["maa.event_sink"]
        del modules["maa"].event_sink
    elif fault == "missing-tasker-base":
        del modules["maa.tasker"].TaskerEventSink
    elif fault == "bad-registration":
        server.register_custom_action(name="bad", unexpected=object())
    elif fault == "business-import":
        sys.meta_path.insert(0, BlockBusinessImport())

    sys.path.insert(0, str(ROOT / "agent"))
    from utils import runtime_environment as runtime
    from utils.persistent_store import PersistentStore

    assert (ROOT / "requirements.txt").is_file(), "This test must exercise a development checkout"
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory).resolve()
        native = root / "native"
        native.mkdir()
        for name in ("libMaaFramework.so", "libMaaAgentServer.so"):
            (native / name).touch()
        stack.enter_context(patch.dict(os.environ, {
            "PI_CLIENT_NAME": "MaaFwApp", "MAAFW_BINARY_PATH": str(native),
            "MFABD2_DATA_DIR": str(root / "save"),
        }, clear=True))
        stack.enter_context(patch.object(runtime.platform, "system", return_value="Linux"))
        venv = stack.enter_context(patch("utils.venv_ops.ensure_venv", side_effect=forbidden))
        stack.enter_context(patch("subprocess.Popen", side_effect=forbidden))
        stack.enter_context(patch("threading.Thread.start", side_effect=forbidden))
        stack.enter_context(patch("socket.socket", side_effect=forbidden))
        # Observe the call count without replacing the real storage policy code.
        configure = stack.enter_context(patch.object(
            PersistentStore, "configure_storage", wraps=PersistentStore.configure_storage))
        namespace = runpy.run_path(str(ROOT / "agent/main.py"), run_name="bootstrap_test")
        venv.assert_not_called()
        config = namespace["runtime"]
        assert config.mode == "android" and not config.manage_venv
        assert config.library_dir == native
        assert os.environ["MAAFW_BINARY_PATH"] == str(native)
        assert PersistentStore._storage_policy == config.storage
        configure.assert_called_once_with(config.storage)
        assert config.storage.directory == root / "save"
        assert not PersistentStore._initialized, "Import must not load user saves"
        assert not (root / "save").exists(), "Import must not write user saves"
        if fault == "missing-registration":
            server._custom_action_holder.pop("FishingAction")
        counts = verify_business_sources(server)
        check_log_events()
    print(f"Simulated Android bootstrap passed: {counts}")


def signature_shape(callable_):
    return [(p.name, p.kind, p.default) for p in inspect.signature(callable_).parameters.values()]


def verify_stub_contract():
    """Never installs the stub: compare its declared surface to the real SDK."""
    for module_name, exports in EXPORTS.items():
        actual_module = importlib.import_module(module_name)
        for name, stub_type in exports.items():
            getattr(actual_module, name)  # Missing real modules/types must fail.
            if name == "NotificationType":
                assert dict(stub_type.__members__) == dict(actual_module.NotificationType.__members__)
    from maa.agent.agent_server import AgentServer
    from android_test_support import AgentServer as StubServer
    for name in ("_set_api_properties", "custom_action", "register_custom_action",
                 "custom_recognition", "register_custom_recognition", "context_sink", "add_context_sink"):
        assert signature_shape(getattr(StubServer, name)) == signature_shape(getattr(AgentServer, name)), name
    for module_name, class_name, members in (
        ("maa.custom_action", "CustomAction", ("RunArg", "RunResult", "run")),
        ("maa.custom_recognition", "CustomRecognition", ("AnalyzeArg", "AnalyzeResult", "analyze")),
        ("maa.context", "ContextEventSink", ("on_node_pipeline_node",)),
        ("maa.tasker", "TaskerEventSink", ("on_tasker_task",)),
    ):
        real = getattr(importlib.import_module(module_name), class_name)
        stub = EXPORTS[module_name][class_name]
        for name in members:
            left, right = getattr(stub, name), getattr(real, name)
            assert signature_shape(left) == signature_shape(right), f"{class_name}.{name}"
            if dataclasses.is_dataclass(left):
                assert [f.name for f in dataclasses.fields(left)] == [f.name for f in dataclasses.fields(right)]
    print("Maa stub import/registration contract matches the installed binding")


def real_binding(python_version, maafw_version):
    assert platform.python_version() == python_version, (platform.python_version(), python_version)
    assert version("MaaFw") == maafw_version, (version("MaaFw"), maafw_version)
    settings = json.loads((ROOT / "android/release.json").read_text(encoding="utf-8"))
    for name, constraint in settings["python_requirements"].items():
        assert constraint.startswith("=="), f"Expected exact Android dependency pin: {name}"
        assert version(name) == constraint[2:], (name, version(name), constraint)
    print(f"Python={platform.python_version()} MaaFw={version('MaaFw')} NumPy={version('numpy')}")
    verify_stub_contract()
    # No environment marker or simulated library path may contaminate this check.
    for name in ("MAAFW_BINARY_PATH", "MAA_LIBRARY_DIR", "PI_CLIENT_NAME", "MFA_ANDROID_OUTPUT_BRIDGED"):
        assert name not in os.environ, f"Unexpected host override: {name}"
    sys.path.insert(0, str(ROOT / "agent"))
    from maa.agent.agent_server import AgentServer
    # Observe return events without replacing any SDK or business function.
    # Holders alone are insufficient: the SDK keeps them even when native
    # registration fails, and re-registering would itself cause duplicates.
    registration_codes = {
        AgentServer.register_custom_action.__code__: "custom_action",
        AgentServer.register_custom_recognition.__code__: "custom_recognition",
    }
    registration_results = []

    def observe_registration(frame, event, result):
        if event == "return" and frame.f_code in registration_codes:
            registration_results.append((registration_codes[frame.f_code], frame.f_locals["name"], result))

    previous_profile = sys.getprofile()
    sys.setprofile(observe_registration)
    try:
        import action
        import recognition
        import fishing_agent
        from utils.log_event_sink import LogEventSink
    finally:
        sys.setprofile(previous_profile)
    maa_root = Path(distribution("MaaFw").locate_file("maa")).resolve()
    for name in EXPORTS:
        source = Path(sys.modules[name].__file__).resolve()
        assert source.is_relative_to(maa_root), (name, source)
    counts = verify_business_sources(AgentServer)
    assert len(registration_results) == counts["custom_action"] + counts["custom_recognition"]
    for kind, name, succeeded in registration_results:
        assert succeeded is True, f"Native {kind} registration failed: {name}"
    check_log_events()
    print(f"Real host MaaFw imports and native callback registration passed: {counts}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bootstrap", "real"))
    parser.add_argument("--fault", choices=("missing-event-module", "missing-tasker-base", "bad-registration",
                                          "missing-registration", "business-import"))
    parser.add_argument("--python-version")
    parser.add_argument("--maafw-version")
    args = parser.parse_args()
    if args.mode == "bootstrap":
        bootstrap(args.fault)
    else:
        if not args.python_version or not args.maafw_version or args.fault:
            parser.error("real requires --python-version and --maafw-version, without fault injection")
        real_binding(args.python_version, args.maafw_version)


if __name__ == "__main__":
    main()
