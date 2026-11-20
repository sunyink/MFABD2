"""Account binding must succeed before weekly completion checks or route work."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steal_test_helpers import load_action


class AccountAdapterTests(unittest.TestCase):
    def setUp(self):
        self.module = load_action("steal_weekly")
        self.context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
        self.args = SimpleNamespace(custom_action_param={})

    def test_binding_failure_never_checks_records_or_constructs_runner(self):
        self.module.sync_from_context.return_value = False
        with patch.object(self.module, "WeeklyRunner") as factory:
            self.assertFalse(self.module.StealWeeklyAction().run(self.context, self.args))
        factory.assert_not_called()
        self.module.manager.check_availability.assert_not_called()

    def test_stop_prevents_binding_and_runner(self):
        self.context.tasker.stopping = True
        with patch.object(self.module, "WeeklyRunner") as factory:
            self.assertFalse(self.module.StealWeeklyAction().run(self.context, self.args))
        factory.assert_not_called()
        self.module.sync_from_context.assert_not_called()

    def test_skip_false_is_passed_to_the_thirteen_route_runner(self):
        report = SimpleNamespace(succeeded=True)
        runner = SimpleNamespace(run=Mock(return_value=report))
        with patch.object(self.module, "WeeklyRunner", return_value=runner), patch.object(self.module, "log_report"):
            self.assertTrue(self.module.StealWeeklyAction().run(self.context,
                SimpleNamespace(custom_action_param={"skip_completed": False})))
        self.assertEqual(len(runner.run.call_args.args[0]), 13)
        self.assertIs(runner.run.call_args.kwargs["skip_completed"], False)

    def test_account_rebinding_failure_blocks_even_a_cached_completion_check(self):
        module = self.module
        module.sync_from_context.side_effect = [True, False]
        class CheckRunner:
            def __init__(self, context, completed, logger):
                self.completed = completed
            def run(self, routes, skip_completed):
                self.completed(routes[0])
                raise AssertionError("Failed binding must stop before this point")
        with patch.object(module, "WeeklyRunner", CheckRunner):
            self.assertFalse(module.StealWeeklyAction().run(self.context, self.args))
        module.manager.check_availability.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
