"""Offline checks for controller-specific StartGame action dispatch."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from startup.common import Prepared


class StartupActionTests(unittest.TestCase):
    def setUp(self):
        self.guard = types.SimpleNamespace(ensure=Mock(return_value=Prepared()))
        modules = {}
        for name in ("maa", "maa.agent", "maa.agent.agent_server", "maa.custom_action", "startup.sink", "utils"):
            modules[name] = types.ModuleType(name)
        modules["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(custom_action=lambda name: lambda cls: cls)
        modules["maa.custom_action"].CustomAction = object
        modules["startup.sink"].guard = self.guard
        modules["utils"].mfaalog = types.SimpleNamespace(info=Mock(), error=Mock())
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("test_startup_adapter", ROOT / "agent/action/startup_prepare.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)

    def context(self, kind):
        controller = types.SimpleNamespace(info={"type": kind}, post_start_app=Mock())
        return types.SimpleNamespace(tasker=types.SimpleNamespace(controller=controller, stopping=False, post_stop=Mock()))

    def test_playcover_continues_manual_game_without_start_app(self):
        context = self.context("playcover")
        self.assertTrue(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_not_called()
        context.tasker.post_stop.assert_not_called()

    def test_prepared_adb_and_pc_do_not_launch_again(self):
        for kind in ("adb", "win32"):
            context = self.context(kind)
            self.assertTrue(self.module.StartupRunApp().run(context, None))
            context.tasker.controller.post_start_app.assert_not_called()

    def test_other_controller_keeps_its_start_app_path(self):
        context = self.context("custom")
        context.tasker.controller.post_start_app.return_value = types.SimpleNamespace(done=True, succeeded=True)
        self.assertTrue(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_called_once_with("com.neowizgames.game.browndust2")

    def test_failed_guard_is_not_bypassed(self):
        self.guard.ensure.return_value = None
        context = self.context("playcover")
        self.assertFalse(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
