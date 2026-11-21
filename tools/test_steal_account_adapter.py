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
            def __init__(self, context, completed, logger, card_return=None):
                self.completed = completed
                self.card_return = card_return
            def run(self, routes, skip_completed):
                self.completed(routes[0])
                raise AssertionError("Failed binding must stop before this point")
        with patch.object(module, "WeeklyRunner", CheckRunner):
            self.assertFalse(module.StealWeeklyAction().run(self.context, self.args))
        module.manager.check_availability.assert_not_called()

    def test_adapter_wires_card_return_callback_into_weekly_runner(self):
        report = SimpleNamespace(succeeded=True)
        runner = SimpleNamespace(run=Mock(return_value=report))
        with patch.object(self.module, "WeeklyRunner", return_value=runner) as factory, \
                patch.object(self.module, "log_report"), \
                patch.object(self.module, "return_card_to_portal", return_value="restored") as ret:
            self.assertTrue(self.module.StealWeeklyAction().run(self.context, self.args))
            callback = factory.call_args.kwargs["card_return"]
            self.assertEqual(callback(3), "restored")
            ret.assert_called_once_with(self.context, 3)

    def test_return_card_to_portal_direct_success_skips_outdoor_board(self):
        module = self.module
        ctx = SimpleNamespace(tasker=SimpleNamespace(stopping=False), clone=lambda: ctx)
        runner = SimpleNamespace(run=Mock(return_value=True), result="restored")
        runner_factory = Mock(return_value=runner)
        navigate_outdoor = Mock()
        result = module.return_card_to_portal(
            ctx, 8, runner_factory=runner_factory, navigate_outdoor=navigate_outdoor, logger=Mock())
        self.assertEqual(result, "restored")
        runner_factory.assert_called_once_with(ctx, skip_if_at_portal=False)
        navigate_outdoor.assert_not_called()

    def test_return_card_to_portal_indoor_unavailable_exits_to_board_and_retries(self):
        module = self.module
        ctx = SimpleNamespace(tasker=SimpleNamespace(stopping=False), clone=lambda: ctx)
        first_runner = SimpleNamespace(
            run=Mock(side_effect=module.ReturnTeleportUnavailable("当前场景不可使用生成魔法阵技能（如室内）；不会改为徒步")),
            capture=Mock(return_value="frame"),
            close_known_menu=Mock(),
            result=None,
        )
        second_runner = SimpleNamespace(run=Mock(return_value=True), result="restored")
        runner_factory = Mock(side_effect=[first_runner, second_runner])
        navigate_outdoor = Mock()
        result = module.return_card_to_portal(
            ctx, 3, runner_factory=runner_factory, navigate_outdoor=navigate_outdoor, logger=Mock())
        self.assertEqual(result, "restored")
        first_runner.close_known_menu.assert_called_once_with("frame")
        navigate_outdoor.assert_called_once_with(ctx, 3)
        self.assertEqual(runner_factory.call_count, 2)

    def test_return_card_to_portal_unconfigured_skill_fails_fast_without_board_navigation(self):
        module = self.module
        ctx = SimpleNamespace(tasker=SimpleNamespace(stopping=False), clone=lambda: ctx)
        first_runner = SimpleNamespace(
            run=Mock(side_effect=module.ReturnTeleportUnavailable("第3技能页未配置生成魔法阵；请先配置该技能")),
            capture=Mock(return_value="frame"),
            close_known_menu=Mock(),
            result=None,
        )
        navigate_outdoor = Mock()
        with self.assertRaises(module.ReturnTeleportUnavailable):
            module.return_card_to_portal(
                ctx, 3, runner_factory=Mock(return_value=first_runner),
                navigate_outdoor=navigate_outdoor, logger=Mock())
        navigate_outdoor.assert_not_called()


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.001, seconds)


class _FakeFrame:
    size = 720 * 1280 * 3
    shape = (720, 1280, 3)

    def __init__(self, hits):
        self.hits = frozenset(hits)


class _WorldHandContext:
    boxes = {
        "SkillButton": [1091, 459, 64, 65],
        "MapPortal": [500, 250, 20, 20],
        "Generate": [672, 389, 61, 64],
        "PortalWorldHand": [630, 289, 22, 14],
        "Back": [80, 22, 20, 20],
    }

    def __init__(self, *, bubble_outcome="map"):
        field = {"FieldHud", "Page3Ready", "SkillConfigured", "SkillReady", "SkillButton"}
        self.states = {
            "field": field,
            "world_hand_only": field | {"PortalWorldHand"},
            "map": {"MapMenu", "MapPortal", "Back"},
            "confirm": {"ConfirmDialog", "ConfirmTitle", "Generate"},
            "interact_map": {"MapMenu", "Back"},
            "npc_menu": {"FieldHud", "NpcMenu", "Back"},
        }
        self.state = "field"
        self.bubble_outcome = bubble_outcome
        self.actions = []
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=SimpleNamespace(
                info={"type": "win32"},
                post_screencap=lambda: SimpleNamespace(
                    done=True,
                    succeeded=True,
                    get=lambda: _FakeFrame(self.states[self.state]),
                ),
            ),
        )

    def clone(self):
        return self

    def run_recognition(self, name, frame):
        suffix = name[len("ReturnTP_"):]
        hit = suffix in frame.hits
        return SimpleNamespace(hit=hit, box=self.boxes.get(suffix, [1, 1, 2, 2]) if hit else None)

    def run_action(self, name, pipeline_override):
        target = pipeline_override[name]["target"]
        self.actions.append(target[:2])
        point = target[:2]
        if point == [1123, 491]:
            self.state = "map"
        elif point == [510, 260]:
            self.state = "confirm"
        elif point == [702, 421]:
            self.state = "world_hand_only"
        elif point == [641, 296]:
            if self.bubble_outcome == "map":
                self.state = "interact_map"
            elif self.bubble_outcome == "npc":
                self.state = "npc_menu"
        elif point == [90, 32]:
            self.state = "world_hand_only"
        return SimpleNamespace(success=True)


class ReturnTeleportWorldHandTests(unittest.TestCase):
    def setUp(self):
        self.module = load_action("return_teleport")
        self.clock = _FakeClock()
        self.orig_time = self.module.time
        self.module.time = self.clock

    def tearDown(self):
        self.module.time = self.orig_time

    def test_post_teleport_world_hand_fallback_succeeds_after_real_map_proof(self):
        ctx = _WorldHandContext(bubble_outcome="map")
        runner = self.module.ReturnTeleportRunner(ctx, skip_if_at_portal=False)
        self.assertTrue(runner.run())
        self.assertEqual(runner.result, "restored")
        self.assertEqual(ctx.actions, [[1123, 491], [510, 260], [702, 421], [641, 296], [90, 32]])

    def test_post_teleport_world_hand_without_map_transition_fails(self):
        ctx = _WorldHandContext(bubble_outcome="none")
        runner = self.module.ReturnTeleportRunner(ctx, skip_if_at_portal=False)
        with self.assertRaisesRegex(self.module.ReturnTeleportUnavailable, "传送阵交互地图"):
            runner.run()
        self.assertIsNone(runner.result)
        self.assertEqual(ctx.actions, [[1123, 491], [510, 260], [702, 421], [641, 296]])

    def test_post_teleport_world_hand_opening_npc_menu_closes_and_fails(self):
        ctx = _WorldHandContext(bubble_outcome="npc")
        runner = self.module.ReturnTeleportRunner(ctx, skip_if_at_portal=False)
        with self.assertRaisesRegex(self.module.ReturnTeleportUnavailable, "NPC菜单"):
            runner.run()
        self.assertIsNone(runner.result)
        self.assertEqual(ctx.actions, [[1123, 491], [510, 260], [702, 421], [641, 296], [90, 32]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
