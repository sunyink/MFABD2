"""Standard-library-only loading helpers for isolated stealing regressions."""

from dataclasses import dataclass, field
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/resource/base"
PC = ROOT / "assets/resource/pc"


@dataclass
class RecognitionDetail:
    name: str
    hit: bool = True
    reco_id: int = 1
    algorithm: str = "OCR"
    box: object = None
    best_result: object = None
    filtered_results: list = field(default_factory=list)


def recognition(name, hit=True, *, algorithm="OCR", children=None, **kwargs):
    if children is not None:
        kwargs["best_result"] = SimpleNamespace(sub_results=children)
    return RecognitionDetail(name, hit=hit, algorithm=algorithm, **kwargs)


def load_action(name):
    package_name = "_steal_action_test"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT / "agent/action")]
    modules = {key: ModuleType(key) for key in (
        "maa", "maa.agent", "maa.agent.agent_server", "maa.custom_action", "maa.define",
        "utils", "utils.account_sync", package_name + ".cartridge_lib",
    )}
    modules[package_name] = package
    modules["maa.agent.agent_server"].AgentServer = SimpleNamespace(custom_action=lambda _: lambda cls: cls)
    modules["maa.custom_action"].CustomAction = type("CustomAction", (), {})
    modules["maa.define"].RecognitionDetail = RecognitionDetail
    modules["utils"].mfaalog = SimpleNamespace(**{level: Mock() for level in ("info", "error", "debug", "warning")})
    modules["utils.account_sync"].sync_from_context = Mock(return_value=True)
    modules[package_name + ".cartridge_lib"].manager = SimpleNamespace(check_availability=Mock(return_value=True))
    old = {key: value for key, value in sys.modules.items() if key.startswith(package_name)}
    for key in old:
        del sys.modules[key]
    try:
        with patch.dict(sys.modules, modules):
            return importlib.import_module(package_name + "." + name)
    finally:
        for key in list(sys.modules):
            if key.startswith(package_name):
                del sys.modules[key]
        sys.modules.update(old)
