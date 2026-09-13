"""Manual offline startup contract tests; Python 3.10+, no native imports."""

import importlib.abc
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


class NoNativeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "action" or fullname.startswith("action.") or fullname in (
            "startup.sink", "startup.win32", "maa"
        ) or fullname.startswith("maa."):
            raise AssertionError("Offline test attempted native import: " + fullname)
        return None


sys.meta_path.insert(0, NoNativeImports())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from startup import adb, pc, guard
from startup.common import Budget, Cancelled, Prepared, PreparationError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative sleep")
        self.now += seconds


class Job:
    def __init__(self, output=None, *, done=True, succeeded=True):
        self.output = output
        self.done = done
        self.succeeded = succeeded

    def get(self):
        if not self.done:
            raise AssertionError("read unfinished job")
        return self.output

    def wait(self):
        raise AssertionError("blocking wait is forbidden")


class Controller:
    def __init__(self, respond=None, *, kind="adb", uuid="device-1"):
        self.info = {"type": kind, "hwnd": 7}
        self.uuid = uuid
        self.respond = respond
        self.commands = []

    def post_shell(self, command, timeout):
        self.commands.append((command, timeout))
        answer = self.respond(command)
        return answer if isinstance(answer, Job) else Job(answer)


class NativeAPI:
    """Only the injected PC surface; never loads the Windows adapter."""
    def __init__(self, frames=None, *, valid=True, size=pc.TARGET):
        self.frames = list(frames or [([7], True, [])])
        self.valid = valid
        self.size = size
        self.scans = self.launches = self.resizes = 0
        self.handles = []

    def scan(self):
        self.scans += 1
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def launch(self):
        self.launches += 1

    def is_game(self, hwnd):
        self.handles.append(hwnd)
        return self.valid

    def restore(self, hwnd):
        self.handles.append(hwnd)

    def fullscreen(self, hwnd):
        return False

    def minimized(self, hwnd):
        return False

    def pseudo_minimized(self, hwnd):
        return False

    def client_size(self, hwnd):
        return self.size

    def resize_client(self, hwnd, target):
        self.resizes += 1


class Tasker:
    def __init__(self, controller):
        self.controller = controller
        self.stopping = False
        self.stops = 0

    def post_stop(self):
        self.stops += 1
        return Job()


class Context:
    def __init__(self, controller, task_id=1):
        self.tasker = Tasker(controller)
        self.task_id = task_id

    def get_task_job(self):
        return SimpleNamespace(job_id=self.task_id)

    def get_node_object(self, name):
        if name != "StartGame_PCWindowOptions":
            raise AssertionError("Unexpected node: " + name)
        return SimpleNamespace(attach={})


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.messages = []

    def budget(self, report=None, cancelled=lambda: False, **kwargs):
        return Budget(self.messages.append if report is None else report,
                      cancelled, clock=self.clock, sleep=self.clock.sleep, **kwargs)


class BudgetTests(ContractTest):
    def test_success_report_does_not_revoke_readiness_at_deadline(self):
        budget = self.budget()
        self.clock.now = 300.1
        budget.success()
        self.assertIn("完成", self.messages[-1])

    def test_success_still_honors_cancellation(self):
        budget = self.budget(cancelled=lambda: True)
        with self.assertRaises(Cancelled):
            budget.success()

    def test_total_timeout_and_30_through_270_progress(self):
        budget = self.budget()
        with self.assertRaises(PreparationError):
            budget.pause(1000)
        self.assertEqual(self.clock.now, 300)
        progress = [int(re.search(r"已等待 (\d+) 秒", text)[1])
                    for text in self.messages if "已等待" in text]
        self.assertEqual(progress, list(range(30, 300, 30)))
        self.assertFalse(any("已等待 300" in text for text in self.messages))

    def test_phases_share_one_deadline(self):
        budget = self.budget()
        budget.pause(200)
        budget.phase = "second phase"
        with self.assertRaises(PreparationError):
            budget.pause(101)
        self.assertEqual(self.clock.now, 300)

    def test_cancel_during_pause(self):
        budget = self.budget(cancelled=lambda: self.clock.now >= 1)
        with self.assertRaises(Cancelled):
            budget.pause(300)
        self.assertLess(self.clock.now, 1.2)


class ADBTests(ContractTest):
    def test_unrelated_diagnostic_text_does_not_mask_foreground(self):
        output = "last exception: background error: not found\n" + f"mCurrentFocus=Window{{123 u0 {adb.PACKAGE}/.Main}}"
        self.assertEqual(adb.parse_foreground(output, ("mCurrentFocus=",)), adb.Foreground.GAME)
        self.assertEqual(adb.parse_foreground("mCurrentFocus=Window{123 u0 com.exception.launcher/.Main}", ("mCurrentFocus=",)), adb.Foreground.OTHER)
        self.assertEqual(adb.parse_foreground("Permission Denial: blocked\n" + output, ("mCurrentFocus=",)), adb.Foreground.UNKNOWN)

    GAME = "mCurrentFocus=Window{1 " + adb.PACKAGE + "/.MainActivity}"
    OTHER = "mCurrentFocus=Window{1 com.android.launcher/.Home}"

    def test_already_foreground_does_not_launch(self):
        controller = Controller(lambda cmd: self.GAME)
        self.assertFalse(adb.prepare(controller, self.budget()).changed)
        self.assertEqual([c for c, _ in controller.commands], ["dumpsys window windows"])

    def test_manual_foreground_after_unknown_uses_loading_branch(self):
        outputs = iter(["mCurrentFocus=null", "unknown", self.GAME])
        controller = Controller(lambda cmd: next(outputs))
        self.assertTrue(adb.prepare(controller, self.budget()).changed)
        self.assertTrue(all(cmd.startswith("dumpsys") for cmd, _ in controller.commands))

    def test_unknown_never_launches(self):
        controller = Controller(lambda cmd: "Permission Denial: inaccessible")
        with self.assertRaises(PreparationError):
            adb.prepare(controller, self.budget())
        self.assertTrue(all(c.startswith("dumpsys ") for c, _ in controller.commands))
        self.assertEqual(self.clock.now, 300)

    def test_am_then_monkey_each_once_even_if_game_never_arrives(self):
        def respond(command):
            if command.startswith("dumpsys"):
                return self.OTHER
            if command.startswith("cmd package"):
                return adb.PACKAGE + "/.MainActivity"
            return "Error: launch failed"
        controller = Controller(respond)
        with self.assertRaises(PreparationError):
            adb.prepare(controller, self.budget())
        launches = [c.split()[0] for c, _ in controller.commands
                    if c.startswith(("am ", "monkey "))]
        self.assertEqual(launches, ["am", "monkey"])
        self.assertEqual(sum(c.startswith("cmd package") for c, _ in controller.commands), 1)

    def test_resolve_failure_still_allows_monkey(self):
        launched = []
        def respond(command):
            if command.startswith("dumpsys"):
                return self.GAME if launched else self.OTHER
            if command.startswith("monkey "):
                launched.append(command)
                return "Events injected: 1"
            return "Error: unable to resolve Intent"
        controller = Controller(respond)
        self.assertTrue(adb.prepare(controller, self.budget()).changed)
        self.assertEqual(len(launched), 1)
        self.assertFalse(any(c.startswith("am ") for c, _ in controller.commands))

    def test_error_and_conflicting_foreground_are_unknown(self):
        for output in (None, "", "Error: " + self.OTHER,
                       "SecurityException " + self.GAME,
                       self.GAME + "\n" + self.OTHER,
                       "mCurrentFocus=null"):
            with self.subTest(output=output):
                self.assertEqual(adb.parse_foreground(output, ("mCurrentFocus=",)),
                                 adb.Foreground.UNKNOWN)

    def test_failed_job_output_cannot_become_other_foreground(self):
        controller = Controller(lambda cmd: Job(self.OTHER, succeeded=False))
        self.assertEqual(adb.foreground(controller, self.budget()), adb.Foreground.UNKNOWN)

    def test_activity_fallback_can_confirm_foreground(self):
        controller = Controller(lambda cmd: "Error: no window permission" if "window" in cmd
                                else "topResumedActivity=ActivityRecord{ " + adb.PACKAGE + "/.Main}")
        self.assertEqual(adb.foreground(controller, self.budget()), adb.Foreground.GAME)

    def test_unfinished_job_cannot_queue_followup(self):
        controller = Controller(lambda cmd: Job(done=False))
        with self.assertRaises(PreparationError):
            adb.prepare(controller, self.budget())
        self.assertEqual(len(controller.commands), 1)
        self.assertLessEqual(self.clock.now, 5.1)

    def test_cancellation_with_unfinished_job_does_not_queue(self):
        controller = Controller(lambda cmd: Job(done=False))
        with self.assertRaises(Cancelled):
            adb.prepare(controller, self.budget(cancelled=lambda: self.clock.now >= 0.5))
        self.assertEqual(len(controller.commands), 1)
        self.assertLess(self.clock.now, 0.7)


class PCTests(ContractTest):
    def test_old_launcher_error_keeps_waiting_for_main_window(self):
        api = NativeAPI([([], True, ["ERROR"]), ([], True, ["ERROR"]), ([7], True, [])])
        pc.prepare(api, self.budget())
        self.assertEqual(api.launches, 0)
        self.assertEqual(api.scans, 3)
        self.assertTrue(any("ERROR" in text for text in self.messages))

    def test_existing_verified_window_is_accepted_immediately(self):
        api = NativeAPI()
        pc.prepare(api, self.budget())
        self.assertEqual((api.launches, api.scans, self.clock.now), (0, 1, 0))

    def test_fullscreen_toggle_is_not_repeated_when_unresponsive(self):
        api = NativeAPI()
        api.toggles = 0
        api.fullscreen = lambda hwnd: True
        def toggle(hwnd):
            api.toggles += 1
        api.exit_fullscreen = toggle
        with self.assertRaises(PreparationError):
            pc.prepare(api, self.budget(), hwnd=7)
        self.assertEqual(api.toggles, 1)
        self.assertEqual(api.resizes, 0)
        self.assertLessEqual(self.clock.now, 10)

    def test_minimized_window_is_not_accepted_until_restored(self):
        api = NativeAPI()
        states = iter([False, False, True])
        api.restore = lambda hwnd: next(states)
        pc.prepare(api, self.budget(), hwnd=7)
        self.assertAlmostEqual(self.clock.now, 0.4)

    def test_fullscreen_exit_then_resize_is_verified(self):
        api = NativeAPI(size=(1920, 1080))
        api.is_fullscreen = True
        api.fullscreen = lambda hwnd: api.is_fullscreen
        api.exit_fullscreen = lambda hwnd: setattr(api, "is_fullscreen", False)
        api.resize_client = lambda hwnd, target: setattr(api, "size", target)
        pc.prepare(api, self.budget(), hwnd=7)
        self.assertFalse(api.is_fullscreen)
        self.assertEqual(api.size, (1280, 720))

    def test_never_launch_twice_while_waiting(self):
        api = NativeAPI([([], False, [])])
        with self.assertRaises(PreparationError):
            pc.prepare(api, self.budget())
        self.assertEqual(api.launches, 1)
        self.assertEqual(self.clock.now, 300)

    def test_main_window_arrives_immediately_after_launch(self):
        api = NativeAPI([([], False, []), ([7], True, [])])
        self.assertTrue(pc.prepare(api, self.budget()).changed)
        self.assertEqual((api.launches, api.scans), (1, 2))
        self.assertLessEqual(self.clock.now, 2)

    def test_multiple_games_rejected_before_mutation(self):
        api = NativeAPI([([7, 8], True, [])])
        with self.assertRaises(PreparationError):
            pc.prepare(api, self.budget())
        self.assertEqual((api.launches, api.resizes, api.handles), (0, 0, []))

    def test_invalid_bound_handle_never_rebinds(self):
        api = NativeAPI([([8], True, [])], valid=False)
        with self.assertRaises(PreparationError):
            pc.prepare(api, self.budget(), hwnd=7)
        self.assertEqual((api.scans, api.launches), (0, 0))
        self.assertEqual(set(api.handles), {7})

    def test_size_readback_failure_is_bounded(self):
        for size in (None, (0, 0), (1920, 1080)):
            with self.subTest(size=size):
                self.clock = Clock()
                api = NativeAPI(size=size)
                with self.assertRaises(PreparationError):
                    pc.prepare(api, self.budget(), hwnd=7)
                self.assertLessEqual(self.clock.now, 10)
                self.assertLessEqual(api.resizes, 20)
                self.assertEqual(api.scans, 0)


class GuardTests(ContractTest):
    def test_identity_failure_does_not_poison_reconnection_or_other_types(self):
        gate = self.make_guard()
        class Unavailable:
            @property
            def info(self):
                raise RuntimeError()
        broken = Context(Unavailable())
        self.assertIsNone(gate.ensure(broken))
        self.assertEqual(broken.tasker.stops, 1)
        self.assertIn("RuntimeError", self.errors[-1])
        self.assertEqual(gate.ensure(Context(Controller(kind="custom"), 2)), Prepared())
        self.assertEqual(gate.ensure(Context(Controller(), 3)), Prepared())
        self.assertEqual(len(self.calls), 1)

    def make_guard(self, prepare=None):
        self.calls = []
        self.errors = []
        def prepared(*args):
            self.calls.append(args)
            return Prepared()
        return guard.StartupGuard(self.messages.append, self.errors.append,
                                  budget_factory=self.budget,
                                  adb_prepare=prepare or prepared, pc_prepare=prepare or prepared)

    def test_actual_type_bypasses_even_if_label_says_pc(self):
        gate = self.make_guard()
        for kind in ("playcover", "custom", "native_android", "", None):
            controller = Controller(kind=kind, uuid=None)
            controller.info["name"] = "PC客户端"
            context = Context(controller)
            self.assertEqual(gate.ensure(context), Prepared())
            self.assertEqual(context.tasker.stops, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.messages, [])

    def test_actual_type_selects_pc_despite_adb_label(self):
        gate = self.make_guard()
        controller = Controller(kind="win32")
        controller.info["name"] = "Adb"
        gate.ensure(Context(controller))
        self.assertEqual(len(self.calls[0]), 4)

    def test_uuid_and_task_id_deduplicate_across_controller_objects(self):
        gate = self.make_guard()
        first = gate.ensure(Context(Controller(uuid="same"), 10))
        second = gate.ensure(Context(Controller(uuid="same"), 10))
        self.assertIs(first, second)
        self.assertEqual(len(self.calls), 1)
        gate.ensure(Context(Controller(uuid="other"), 10))
        self.assertEqual(len(self.calls), 2)

    def test_new_task_rechecks_and_explicit_task_id_is_used(self):
        gate = self.make_guard()
        context = Context(Controller(), 1)
        gate.ensure(context)
        context.task_id = 2
        gate.ensure(context)
        gate.ensure(context, task_id=3)
        gate.ensure(context, task_id=3)
        self.assertEqual(len(self.calls), 3)

    def test_failure_new_task_stops_fast_without_wait(self):
        attempts = []
        def fail(controller, budget):
            attempts.append(controller)
            budget.pause(1)
            raise PreparationError("contract failure")
        gate = self.make_guard(fail)
        context = Context(Controller())
        self.assertIsNone(gate.ensure(context))
        elapsed = self.clock.now
        context.task_id = 2
        self.assertIsNone(gate.ensure(context))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.clock.now, elapsed)
        self.assertEqual(context.tasker.stops, 2)
        self.assertTrue(all("contract failure" in error for error in self.errors))

    def test_cancel_stops_without_poisoning_next_task(self):
        def prepare(controller, budget):
            budget.check()
            return Prepared()
        gate = self.make_guard(prepare)
        context = Context(Controller())
        context.tasker.stopping = True
        self.assertIsNone(gate.ensure(context))
        self.assertEqual(context.tasker.stops, 1)
        context.tasker.stopping = False
        context.task_id = 2
        self.assertEqual(gate.ensure(context), Prepared())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
