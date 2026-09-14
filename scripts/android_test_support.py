"""Strict import-time Maa boundary for Android bootstrap tests, never production.

No business modules are replaced. Only APIs needed during registration exist;
device operations and host startup are deliberately unavailable. The independent
real-binding probe checks these signatures against the installed MaaFw package.
"""

import ast
import importlib.abc
import importlib.machinery
import sys
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Union

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


class NotificationType(IntEnum):
    Unknown = 0
    Starting = 1
    Succeeded = 2
    Failed = 3


@dataclass
class Rect:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


RectType = Union[Rect, list[int], np.ndarray, tuple[int, int, int, int]]


class Context:
    def __init__(self, handle):
        raise AssertionError("A bootstrap test must not create a device Context")


class CustomAction(ABC):
    @dataclass
    class RunArg:
        task_detail: Any
        node_name: str
        custom_action_name: str
        custom_action_param: str
        reco_detail: Any
        box: Rect

    @dataclass
    class RunResult:
        success: bool

    @abstractmethod
    def run(self, context, argv):
        raise NotImplementedError


class CustomRecognition(ABC):
    @dataclass
    class AnalyzeArg:
        task_detail: Any
        node_name: str
        custom_recognition_name: str
        custom_recognition_param: str
        image: np.ndarray
        roi: Rect

    @dataclass
    class AnalyzeResult:
        box: RectType | None
        detail: dict[str, Any]

    @abstractmethod
    def analyze(self, context, argv):
        raise NotImplementedError


class TaskerEventSink:
    def on_tasker_task(self, tasker, noti_type, detail):
        pass


class ContextEventSink:
    def on_node_pipeline_node(self, context, noti_type, detail):
        pass


def forbidden(*args, **kwargs):
    raise AssertionError("External operation attempted during bootstrap import")


class Toolkit:
    # Import is permitted; initialization belongs to main(), not bootstrap.
    init_option = staticmethod(forbidden)


class AgentServer:
    _custom_action_holder = {}
    _custom_recognition_holder = {}
    _sink_holder = {}
    _api_properties_initialized = False

    @staticmethod
    def _set_api_properties():
        AgentServer._api_properties_initialized = True

    @staticmethod
    def register_custom_action(name, action):
        if not isinstance(name, str) or not isinstance(action, CustomAction):
            raise TypeError("register_custom_action requires a name and CustomAction instance")
        if name in AgentServer._custom_action_holder:
            raise AssertionError(f"Duplicate action registration: {name}")
        AgentServer._set_api_properties()
        AgentServer._custom_action_holder[name] = action
        return True

    @staticmethod
    def custom_action(name):
        def wrapper(action):
            AgentServer.register_custom_action(name=name, action=action())
            return action
        return wrapper

    @staticmethod
    def register_custom_recognition(name, recognition):
        if not isinstance(name, str) or not isinstance(recognition, CustomRecognition):
            raise TypeError("register_custom_recognition requires a name and CustomRecognition instance")
        if name in AgentServer._custom_recognition_holder:
            raise AssertionError(f"Duplicate recognition registration: {name}")
        AgentServer._set_api_properties()
        AgentServer._custom_recognition_holder[name] = recognition
        return True

    @staticmethod
    def custom_recognition(name):
        def wrapper(recognition):
            AgentServer.register_custom_recognition(name=name, recognition=recognition())
            return recognition
        return wrapper

    @staticmethod
    def add_context_sink(sink):
        if not isinstance(sink, ContextEventSink):
            raise TypeError("add_context_sink requires a ContextEventSink instance")
        if not AgentServer._api_properties_initialized:
            raise AssertionError("Context sink registration requires API initialization")
        AgentServer._sink_holder[len(AgentServer._sink_holder) + 1] = sink

    @staticmethod
    def context_sink():
        def wrapper(sink):
            AgentServer.add_context_sink(sink=sink())
            return sink
        return wrapper

    start_up = staticmethod(forbidden)
    join = staticmethod(forbidden)
    shut_down = staticmethod(forbidden)


EXPORTS = {
    "maa.agent.agent_server": {"AgentServer": AgentServer},
    "maa.toolkit": {"Toolkit": Toolkit},
    "maa.event_sink": {"NotificationType": NotificationType},
    "maa.tasker": {"TaskerEventSink": TaskerEventSink},
    "maa.context": {"Context": Context, "ContextEventSink": ContextEventSink},
    "maa.custom_action": {"CustomAction": CustomAction},
    "maa.custom_recognition": {"CustomRecognition": CustomRecognition},
    "maa.define": {"Rect": Rect, "RectType": RectType},
}


class BlockUnexpectedMaa(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "maa" or fullname.startswith("maa."):
            raise ModuleNotFoundError(f"Unmodelled Maa module: {fullname}", name=fullname)
        return None


def install_maa_stub():
    if any(name == "maa" or name.startswith("maa.") for name in sys.modules):
        raise AssertionError("Maa stub requires a fresh process, with no real binding cached")
    modules = {}
    for name in ["maa", "maa.agent", *EXPORTS]:
        package = name in {"maa", "maa.agent"}
        module = types.ModuleType(name)
        module.__package__ = name if package else name.rpartition(".")[0]
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=package)
        if package:
            module.__path__ = []
        module.__dict__.update(EXPORTS.get(name, {}))
        modules[name] = module
        sys.modules[name] = module
        if "." in name:
            parent, _, child = name.rpartition(".")
            setattr(modules[parent], child, module)
    # Missing entries must never fall back to site-packages, even in negative tests.
    sys.meta_path.insert(0, BlockUnexpectedMaa())
    return modules


def expected_registrations():
    """Read declared decorators independently of the runtime registration code."""
    expected = {"custom_action": {}, "custom_recognition": {}, "context_sink": {}}
    paths = [ROOT / "agent/fishing_agent.py"]
    for directory in ("action", "recognition", "startup"):
        paths.extend((ROOT / "agent" / directory).glob("*.py"))
    for path in paths:
        if path.name.startswith("test_"):
            continue
        module = ".".join(path.relative_to(ROOT / "agent").with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id == "AgentServer"):
                    continue
                kind = decorator.func.attr
                if kind not in expected:
                    raise AssertionError(f"Unmodelled registration decorator: {kind}")
                identity = f"{module}.{node.name}"
                key = ast.literal_eval(decorator.args[0]) if kind != "context_sink" else identity
                if key in expected[kind]:
                    raise AssertionError(f"Duplicate declared {kind}: {key}")
                expected[kind][key] = identity
    return expected


def verify_business_sources(server):
    expected = expected_registrations()
    actual = {
        "custom_action": {name: f"{type(obj).__module__}.{type(obj).__name__}"
                          for name, obj in server._custom_action_holder.items()},
        "custom_recognition": {name: f"{type(obj).__module__}.{type(obj).__name__}"
                               for name, obj in server._custom_recognition_holder.items()},
        "context_sink": {f"{type(obj).__module__}.{type(obj).__name__}":
                         f"{type(obj).__module__}.{type(obj).__name__}" for obj in server._sink_holder.values()},
    }
    if actual != expected:
        raise AssertionError(f"Registration mismatch: expected={expected}, actual={actual}")
    names = {"action", "recognition", "fishing_agent", "startup.sink", "utils.log_event_sink"}
    for entries in expected.values():
        names.update(identity.rsplit(".", 1)[0] for identity in entries.values())
    for name in names:
        module = sys.modules[name]
        expected_path = ROOT / "agent" / Path(*name.split("."))
        expected_path = expected_path / "__init__.py" if expected_path.is_dir() else expected_path.with_suffix(".py")
        if Path(getattr(module, "__file__", "")).resolve() != expected_path.resolve():
            raise AssertionError(f"Business module did not load from source: {name}")
    return {kind: len(entries) for kind, entries in actual.items()}
