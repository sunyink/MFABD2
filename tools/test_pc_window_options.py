"""Offline contract tests; no production module outside the explicit allowlist loads."""
import argparse
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

DEFAULT_ROOT = Path(__file__).resolve().parents[1]


class BlockUnsafeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('maa', 'action', 'ctypes') or fullname in ('startup.sink', 'startup.win32'):
            raise ImportError('Forbidden in offline tests: ' + fullname)


def load_production(root):
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, BlockUnsafeImports())
    package = types.ModuleType('startup')
    package.__path__ = []
    sys.modules['startup'] = package
    adb = types.ModuleType('startup.adb')
    adb.prepare = Mock(side_effect=AssertionError('Inject a fake ADB implementation'))
    sys.modules['startup.adb'] = adb
    loaded = []
    for name in ('common', 'options', 'pc', 'guard'):
        path = root / 'agent' / 'startup' / (name + '.py')
        spec = importlib.util.spec_from_file_location('startup.' + name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 0.100001
        self.now += seconds
        assert self.now <= 301, 'Unbounded fake wait'


class FakeAPI:
    def __init__(self, size=(800, 600), minimized=False, fullscreen=False, pseudo=False):
        self.size = size
        self.iconic = minimized
        self.full = fullscreen
        self.pseudo = pseudo
        self.calls = []
        self.restore_ok = self.resize_ok = self.exit_ok = self.minimize_ok = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def is_game(self, hwnd):
        return True

    def client_size(self, hwnd):
        self.calls.append(('read', self.size))
        return self.size

    def minimized(self, hwnd):
        return self.iconic

    def fullscreen(self, hwnd):
        return self.full

    def pseudo_minimized(self, hwnd):
        return self.pseudo

    def restore(self, hwnd):
        self.calls.append(('restore',))
        if self.restore_ok:
            self.iconic = False
        return self.restore_ok

    def exit_fullscreen(self, hwnd):
        self.calls.append(('exit',))
        if self.exit_ok:
            self.full = False

    def resize_client(self, hwnd, target):
        self.calls.append(('resize', target))
        if self.resize_ok:
            self.size = target

    def minimize(self, hwnd):
        self.calls.append(('minimize',))
        if self.minimize_ok:
            self.pseudo = True


def context(kind='win32', attach=None):
    ctx = types.SimpleNamespace()
    ctx.tasker = types.SimpleNamespace(
        controller=types.SimpleNamespace(info={'type': kind, 'hwnd': '0x123'}, uuid='fake'),
        stopping=False, post_stop=Mock())
    ctx.get_node_object = Mock(return_value=types.SimpleNamespace(attach={} if attach is None else attach))
    ctx.get_task_job = Mock(return_value=types.SimpleNamespace(job_id=1))
    return ctx


class Contracts(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.logs = []
        self.budget = common.Budget(self.logs.append, clock=self.clock, sleep=self.clock.sleep)

    def prepare(self, api, resolution='720p', minimize=False):
        return pc.prepare(api, self.budget, hwnd=0x123, options=options.PCOptions(resolution, minimize))

    def budget_factory(self, report, cancelled):
        return common.Budget(report, cancelled, clock=self.clock, sleep=self.clock.sleep)

    def test_option_matrix_matches_pretask_and_attach(self):
        for resolution, target in [('720p', (1280, 720)), ('1080p', (1920, 1080))]:
            for label, value in [('Yes', True), ('No', False)]:
                with self.subTest(resolution=resolution, minimize=label):
                    payload = {options.RESOLUTION_OPTION: resolution, options.MINIMIZE_OPTION: label}
                    left = options.PCOptions.from_pretask([json.dumps(payload)])
                    ctx = context(attach={'resolution': resolution, 'minimize': value})
                    self.assertEqual(left, options.PCOptions.from_context(ctx))
                    self.assertEqual(left.target, target)
                    self.assertIs(left.minimize, value)
                    ctx.get_node_object.assert_called_once_with(options.NODE)

    def test_defaults(self):
        expected = options.PCOptions('720p', False)
        for args in ([], ['{}']):
            self.assertEqual(options.PCOptions.from_pretask(args), expected)
        self.assertEqual(options.PCOptions.from_context(context()), expected)

    def test_invalid_values_fail(self):
        bad_pretask = ['broken', '[]', 'null', json.dumps({options.RESOLUTION_OPTION: '1440p'}),
                       json.dumps({options.MINIMIZE_OPTION: True}), json.dumps({options.MINIMIZE_OPTION: 'yes'})]
        for raw in bad_pretask:
            with self.subTest(raw=raw), self.assertRaises(common.PreparationError):
                options.PCOptions.from_pretask([raw])
        with self.assertRaises(common.PreparationError):
            options.PCOptions.from_pretask(['{}', '{}'])
        for attach in ({'resolution': 'bad'}, {'minimize': 'Yes'}, {'minimize': 1}, []):
            with self.subTest(attach=attach), self.assertRaises(common.PreparationError):
                options.PCOptions.from_context(context(attach=attach))
        ctx = context()
        ctx.get_node_object.return_value = None
        with self.assertRaises(common.PreparationError):
            options.PCOptions.from_context(ctx)

    def test_resize_readback_and_sink_log(self):
        for resolution, target in options.RESOLUTIONS.items():
            with self.subTest(resolution=resolution):
                api = FakeAPI()
                self.prepare(api, resolution)
                self.assertEqual(api.size, target)
                index = api.calls.index(('resize', target))
                self.assertIn(('read', target), api.calls[index + 1:])
                self.assertIn(f'[sink] 分辨率 800×600 → {target[0]}×{target[1]}（已调整', self.logs[-1])

    def test_correct_size_explained(self):
        api = FakeAPI(size=(1280, 720))
        self.prepare(api)
        self.assertFalse(any(c[0] == 'resize' for c in api.calls))
        self.assertIn('[sink]', self.logs[-1])
        self.assertIn('无需调整', self.logs[-1])

    def test_failed_resize_is_not_reported_success(self):
        api = FakeAPI()
        api.resize_ok = False
        with self.assertRaises(common.PreparationError):
            self.prepare(api)
        self.assertLessEqual(self.clock.now, 10)
        self.assertNotIn('[PC窗口]', '\n'.join(self.logs))

    def test_fullscreen_exit(self):
        api = FakeAPI(fullscreen=True)
        self.prepare(api)
        self.assertEqual(api.calls.count(('exit',)), 1)
        self.assertFalse(api.full)
        self.assertIn('已退出全屏', self.logs[-1])

    def test_fullscreen_failure_bounded(self):
        api = FakeAPI(fullscreen=True)
        api.exit_ok = False
        with self.assertRaises(common.PreparationError):
            self.prepare(api)
        self.assertEqual(api.calls.count(('exit',)), 1)
        self.assertLessEqual(self.clock.now, 10)

    def test_minimized_restore(self):
        api = FakeAPI(minimized=True)
        self.prepare(api)
        self.assertFalse(api.iconic)
        self.assertEqual(api.size, (1280, 720))
        self.assertIn('已还原窗口', self.logs[-1])

    def test_restore_failure_bounded(self):
        api = FakeAPI(minimized=True)
        api.restore_ok = False
        with self.assertRaises(common.PreparationError):
            self.prepare(api)
        self.assertLessEqual(self.clock.now, 10)

    def test_minimize_after_resize(self):
        api = FakeAPI()
        self.prepare(api, minimize=True)
        self.assertTrue(api.pseudo)
        self.assertLess(api.calls.index(('read', (1280, 720))), api.calls.index(('minimize',)))

    def test_pretask_defers_minimize_for_both_resolutions(self):
        for resolution, target in options.RESOLUTIONS.items():
            with self.subTest(resolution=resolution):
                api = FakeAPI()
                api.scan = Mock(return_value=([0x123], True, []))
                api.minimize_ok = False
                pc.prepare(api, self.budget, options=options.PCOptions(resolution, True))
                self.assertEqual(api.size, target)
                self.assertNotIn(('minimize',), api.calls)
                self.assertFalse(api.iconic or api.pseudo)
                self.assertIn('连接完成后', self.logs[-1])

    def test_pretask_restores_existing_minimized_window_for_connection(self):
        api = FakeAPI(size=(1280, 720), minimized=True)
        api.scan = Mock(return_value=([0x123], True, []))
        pc.prepare(api, self.budget, options=options.PCOptions(minimize=True))
        self.assertFalse(api.iconic or api.pseudo)
        self.assertIn(('restore',), api.calls)
        self.assertNotIn(('minimize',), api.calls)

    def test_sink_applies_minimize_after_pretask_and_connection(self):
        api = FakeAPI()
        api.scan = Mock(return_value=([0x123], True, []))
        requested = options.PCOptions('1080p', True)
        pc.prepare(api, self.budget, options=requested)
        self.assertFalse(api.iconic or api.pseudo)
        self.assertNotIn(('minimize',), api.calls)
        pc.prepare(api, self.budget, hwnd=0x123, options=requested)
        self.assertTrue(api.pseudo)
        self.assertEqual(api.calls.count(('minimize',)), 1)
        self.assertIn('[sink]', self.logs[-1])
        self.assertIn('最小化', self.logs[-1])

    def test_minimize_failure_bounded(self):
        api = FakeAPI(size=(1280, 720))
        api.minimize_ok = False
        with self.assertRaisesRegex(common.PreparationError, '2 秒内未确认 PC 窗口最小化状态'):
            self.prepare(api, minimize=True)
        self.assertLessEqual(self.clock.now, 2.11)
        self.assertEqual(api.calls.count(('minimize',)), 1)

    def test_pseudo_minimized_correct_size_not_minimized_again(self):
        api = FakeAPI(size=(1280, 720), pseudo=True)
        self.prepare(api, minimize=True)
        self.assertNotIn(('minimize',), api.calls)
        self.assertFalse(any(c[0] == 'resize' for c in api.calls))
        self.assertIn('无需调整', self.logs[-1])

    def test_guard_framework_restore_when_minimize_disabled(self):
        api = FakeAPI(size=(1280, 720))
        fake_module = types.ModuleType('startup.win32')
        fake_module.WindowsAPI = lambda: api
        controller = context().tasker.controller
        def inactive():
            api.pseudo = False
            return types.SimpleNamespace(done=True, succeeded=True)
        controller.post_inactive = Mock(side_effect=inactive)
        with patch.dict(sys.modules, {'startup.win32': fake_module}):
            guard.StartupGuard._pc_prepare(controller, controller.info, self.budget, options.PCOptions(minimize=True))
            self.assertTrue(api.pseudo)
            guard.StartupGuard._pc_prepare(controller, controller.info, self.budget, options.PCOptions(minimize=False))
        controller.post_inactive.assert_called_once_with()
        self.assertFalse(api.pseudo)
        self.assertIn('普通窗口', self.logs[-1])

    def test_framework_restore_failure_and_timeout(self):
        for done in (True, False):
            with self.subTest(done=done):
                self.setUp()
                api = FakeAPI(size=(1280, 720), pseudo=True)
                fake_module = types.ModuleType('startup.win32')
                fake_module.WindowsAPI = lambda: api
                controller = context().tasker.controller
                controller.post_inactive = Mock(return_value=types.SimpleNamespace(done=done, succeeded=False))
                with patch.dict(sys.modules, {'startup.win32': fake_module}), self.assertRaises(common.PreparationError):
                    guard.StartupGuard._pc_prepare(controller, controller.info, self.budget, options.PCOptions())
                self.assertLessEqual(self.clock.now, 5.11)

    def test_guard_once_per_task_and_rereads_new_task(self):
        prepare = Mock(return_value=common.Prepared())
        g = guard.StartupGuard(self.logs.append, self.fail, pc_prepare=prepare, budget_factory=self.budget_factory)
        ctx = context()
        self.assertIsNotNone(g.ensure(ctx, 1))
        ctx.get_node_object.return_value.attach = {'resolution': '1080p', 'minimize': True}
        self.assertIsNotNone(g.ensure(ctx, 1))
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(ctx.get_node_object.call_count, 1)
        self.assertIsNotNone(g.ensure(ctx, 2))
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(ctx.get_node_object.call_count, 2)
        self.assertEqual(prepare.call_args_list[0].args[3], options.PCOptions())
        self.assertEqual(prepare.call_args_list[1].args[3], options.PCOptions('1080p', True))

    def test_non_pc_never_reads_pc_node(self):
        for kind in ('adb', 'custom'):
            with self.subTest(kind=kind):
                ctx = context(kind)
                ctx.get_node_object.side_effect = AssertionError('PC node accessed')
                adb_prepare = Mock(return_value=common.Prepared())
                pc_prepare = Mock(side_effect=AssertionError('PC prepare called'))
                g = guard.StartupGuard(self.logs.append, self.fail, adb_prepare=adb_prepare, pc_prepare=pc_prepare,
                                       budget_factory=self.budget_factory)
                self.assertIsNotNone(g.ensure(ctx, 1))
                ctx.get_node_object.assert_not_called()
                pc_prepare.assert_not_called()
                self.assertEqual(adb_prepare.call_count, int(kind == 'adb'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, default=Path(DEFAULT_ROOT))
    args, remaining = parser.parse_known_args()
    common, options, pc, guard = load_production(args.project_root.resolve())
    unittest.main(argv=[sys.argv[0]] + remaining)
