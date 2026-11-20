"""Exercise the production cooldown code without starting Maa or reading saves."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "agent/action/cartridge_lib.py"
BEIJING = timezone(timedelta(hours=8))


def load_cooldown_code():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    strategies = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "CYCLE_STRATEGIES"
                              for target in node.targets))
    manager = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "CooldownManager")
    methods = [node for node in manager.body if isinstance(node, ast.FunctionDef)
               and node.name in ("_calculate_server_reset_timestamp", "_get_storage_key")]
    if len(methods) != 2:
        raise AssertionError("Production cooldown methods were not found")
    selected_class = ast.parse("class CooldownManager:\n    pass\n").body[0]
    selected_class.body = methods
    selected = ast.fix_missing_locations(ast.Module(body=[strategies, selected_class], type_ignores=[]))
    namespace = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone,
                 "utils": SimpleNamespace(mfaalog=Mock())}
    exec(compile(selected, str(SOURCE), "exec"), namespace)
    return namespace


class StealCycleTests(unittest.TestCase):
    def setUp(self):
        self.namespace = load_cooldown_code()
        self.manager = self.namespace["CooldownManager"]()

    def reset_at(self, instant):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    raise AssertionError("Reset calculation must use an explicit server timezone")
                return instant.astimezone(tz)
        self.namespace["datetime"] = FrozenDateTime
        timestamp, strategy = self.manager._calculate_server_reset_timestamp("steal_weekly")
        self.assertIs(strategy, self.namespace["CYCLE_STRATEGIES"]["steal_weekly"])
        self.namespace["utils"].mfaalog.warning.assert_not_called()
        return datetime.fromtimestamp(timestamp, BEIJING)

    def test_monday_before_eight_uses_previous_week(self):
        self.assertEqual(self.reset_at(datetime(2026, 10, 26, 7, 59, 59, tzinfo=BEIJING)),
                         datetime(2026, 10, 19, 8, tzinfo=BEIJING))

    def test_monday_eight_starts_new_week(self):
        self.assertEqual(self.reset_at(datetime(2026, 10, 26, 8, tzinfo=BEIJING)),
                         datetime(2026, 10, 26, 8, tzinfo=BEIJING))

    def test_thursday_does_not_reset(self):
        for hour in (7, 8, 23):
            with self.subTest(hour=hour):
                self.assertEqual(self.reset_at(datetime(2026, 10, 29, hour, tzinfo=BEIJING)),
                                 datetime(2026, 10, 26, 8, tzinfo=BEIJING))

    def test_new_year_monday_before_reset_uses_december(self):
        self.assertEqual(self.reset_at(datetime(2024, 1, 1, 7, 59, tzinfo=BEIJING)),
                         datetime(2023, 12, 25, 8, tzinfo=BEIJING))

    def test_new_year_monday_reset_boundary(self):
        self.assertEqual(self.reset_at(datetime(2024, 1, 1, 8, tzinfo=BEIJING)),
                         datetime(2024, 1, 1, 8, tzinfo=BEIJING))

    def test_utc_midnight_is_beijing_monday_eight(self):
        self.assertEqual(self.reset_at(datetime(2026, 10, 26, 0, tzinfo=timezone.utc)),
                         datetime(2026, 10, 26, 8, tzinfo=BEIJING))

    def test_dedicated_strategy_and_existing_save_key_remain_compatible(self):
        strategy = self.namespace["CYCLE_STRATEGIES"]["steal_weekly"]
        self.assertEqual(strategy["type"], "weekly")
        self.assertEqual(strategy["blackout_minutes"], 0)
        self.assertEqual(self.manager._get_storage_key("Steal_C2_LoanShark", "steal_weekly"),
                         "Steal_C2_LoanShark@steal_weekly")


if __name__ == "__main__":
    unittest.main(verbosity=2)
