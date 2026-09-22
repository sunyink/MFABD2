"""Offline contract tests; no production module outside the explicit allowlist loads."""
import argparse
import importlib.abc
import importlib.util
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
    def __init__(self, size=(800, 600), minimized=False, maximized=False, fullscreen=False, pseudo=False):
        self.size = size
        self.iconic = minimized
        self.zoomed = maximized
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

    # 默认「没有窗口、游戏没在跑」。pretask 用例按需 Mock 掉；不记入 calls，
    # 因为 calls 是用来断言「动了窗口哪些状态」的。
    def scan(self):
        return ([], False, [])

    def launch(self):
        self.calls.append(('launch',))

    def client_size(self, hwnd):
        size = (0, 0) if self.iconic else self.size
        self.calls.append(('read', size))
        return size

    def minimized(self, hwnd):
        return self.iconic

    def maximized(self, hwnd):
        return self.zoomed

    def fullscreen(self, hwnd):
        return self.full

    def pseudo_minimized(self, hwnd):
        return self.pseudo

    def restore(self, hwnd):
        self.calls.append(('restore',))
        if self.restore_ok:
            self.iconic = False
            self.zoomed = False
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
        self.warnings = []
        self.budget = common.Budget(self.logs.append, clock=self.clock, sleep=self.clock.sleep,
                                    warn=self.warnings.append)

    def prepare(self, api, resolution='720p', minimize=False):
        return pc.prepare(api, self.budget, hwnd=0x123, options=options.PCOptions(resolution, minimize))

    # guard 的 PC 分支会传 timeout/warn，ADB 分支不传；**kwargs 让两边都能过。
    def budget_factory(self, report, cancelled, **kwargs):
        return common.Budget(report, cancelled, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    # ---- 选项解析（只剩 attach 一个入口，pretask 不再消费选项） ----

    def test_option_matrix_from_attach(self):
        for resolution, target in [('720p', (1280, 720)), ('1080p', (1920, 1080))]:
            for value in (True, False):
                with self.subTest(resolution=resolution, minimize=value):
                    ctx = context(attach={'resolution': resolution, 'minimize': value})
                    parsed = options.PCOptions.from_context(ctx)
                    self.assertEqual(parsed.target, target)
                    self.assertIs(parsed.minimize, value)
                    ctx.get_node_object.assert_called_once_with(options.NODE)

    def test_defaults(self):
        self.assertEqual(options.PCOptions.from_context(context()), options.PCOptions('720p', False))

    def test_invalid_values_fail(self):
        for attach in ({'resolution': 'bad'}, {'minimize': 'Yes'}, {'minimize': 1}, []):
            with self.subTest(attach=attach), self.assertRaises(common.PreparationError):
                options.PCOptions.from_context(context(attach=attach))
        ctx = context()
        ctx.get_node_object.return_value = None
        with self.assertRaises(common.PreparationError):
            options.PCOptions.from_context(ctx)

    def test_pretask_entry_no_longer_parses_options(self):
        # from_pretask 连同两个中文 option 常量一起删除了；留着就是带测试的死代码。
        self.assertFalse(hasattr(options.PCOptions, 'from_pretask'))
        self.assertFalse(hasattr(options, 'RESOLUTION_OPTION'))
        self.assertFalse(hasattr(options, 'MINIMIZE_OPTION'))

    # ---- 长宽比分级 ----

    def test_aspect_deviation_table(self):
        for size, expected in [((1280, 720), 0.0), ((1920, 1080), 0.0), ((1652, 929), 0.000269),
                               ((1366, 768), 0.000488), ((1280, 800), 0.1)]:
            with self.subTest(size=size):
                self.assertAlmostEqual(pc.aspect_deviation(size), expected, places=5)
        for size in (None, (0, 0), (1280, 0), (1280,), 'x'):
            with self.subTest(size=size):
                self.assertIsNone(pc.aspect_deviation(size))
        # 容差边界：±2% 以内带提示继续跑，超出才停当前任务。
        self.assertLessEqual(pc.aspect_deviation((1280, 706)), pc.ASPECT_TOLERANCE)
        self.assertGreater(pc.aspect_deviation((1280, 705)), pc.ASPECT_TOLERANCE)

    def test_off_size_but_matching_aspect_warns_and_continues(self):
        # 玩家实测尺寸：框架按短边 720 缩放后正好是 1280×720，识别本来就是对的。
        api = FakeAPI(size=(1652, 929))
        api.resize_ok = False
        self.assertIsNotNone(self.prepare(api))
        self.assertLessEqual(self.clock.now, pc.RESIZE_WINDOW + 0.1)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('1652×929', self.warnings[0])
        self.assertIn('短边', self.warnings[0])
        self.assertIn('本任务继续执行', self.warnings[0])
        self.assertNotIn('未达目标', '\n'.join(self.logs))

    def test_off_aspect_fails_current_task(self):
        api = FakeAPI(size=(1280, 800))
        api.resize_ok = False
        with self.assertRaisesRegex(common.PreparationError, '长宽比偏离目标'):
            self.prepare(api)
        self.assertLessEqual(self.clock.now, pc.RESIZE_WINDOW + 0.1)
        self.assertEqual(self.warnings, [])

    # ---- sink 路径：已连接窗口的校正 ----

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
        api = FakeAPI()  # 800×600 是 4:3，偏离目标 25%，仍然致命
        api.resize_ok = False
        with self.assertRaisesRegex(common.PreparationError, '长宽比偏离目标'):
            self.prepare(api)
        self.assertLessEqual(self.clock.now, pc.RESIZE_WINDOW + 0.1)
        self.assertIn('已请求调整', '\n'.join(self.logs))
        self.assertFalse(any('（已调整' in line for line in self.logs + self.warnings))

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
        self.assertLessEqual(self.clock.now, pc.RESIZE_WINDOW + 0.1)

    def test_stuck_fullscreen_but_correct_size_says_so(self):
        # 降级路径下「没退出全屏但客户区恰好达标」，报告必须说实话。
        api = FakeAPI(size=(1280, 720), fullscreen=True)
        api.exit_ok = False
        self.assertIsNotNone(self.prepare(api))
        self.assertIn('仍为全屏', self.warnings[-1])
        self.assertNotIn('已退出全屏', self.warnings[-1])

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
        self.assertLessEqual(self.clock.now, pc.RESIZE_WINDOW + 0.1)

    def test_minimize_after_resize(self):
        api = FakeAPI()
        self.prepare(api, minimize=True)
        self.assertTrue(api.pseudo)
        self.assertLess(api.calls.index(('read', (1280, 720))), api.calls.index(('minimize',)))

    def test_delayed_resize_is_confirmed_before_minimize(self):
        # The real game changed size several seconds after the old five-second
        # resize phase had expired. An accepted native request is not completion.
        for initial in ((2530, 1423), (1152, 648)):
            with self.subTest(initial=initial):
                self.setUp()
                api = FakeAPI(size=initial)
                api.resize_ok = False
                read_size = api.client_size
                applied = []

                def deferred_size(hwnd):
                    if self.clock.now >= 8 and not applied:
                        api.size = (1280, 720)
                        applied.append(self.clock.now)
                    return read_size(hwnd)

                api.client_size = deferred_size
                minimize = api.minimize

                def checked_minimize(hwnd):
                    self.assertTrue(applied, 'Minimized while the resize request was pending')
                    self.assertEqual(api.size, (1280, 720))
                    minimize(hwnd)

                api.minimize = checked_minimize
                self.prepare(api, minimize=True)
                self.assertTrue(api.pseudo, '\n'.join(self.logs))
                self.assertEqual(api.calls.count(('resize', (1280, 720))), 1)
                self.assertGreaterEqual(self.clock.now, 8)

    def test_unconfirmed_resize_does_not_minimize_or_stop_a_usable_window(self):
        api = FakeAPI(size=(1920, 1080))
        api.resize_ok = False
        self.assertIsNotNone(self.prepare(api, minimize=True))
        self.assertNotIn(('minimize',), api.calls)
        self.assertEqual(api.calls.count(('resize', (1280, 720))), 1)
        self.assertIn('本次不再最小化', '\n'.join(self.warnings))

    def test_transient_target_size_does_not_trigger_minimize(self):
        api = FakeAPI(size=(1920, 1080))
        api.resize_ok = False
        read_size = api.client_size

        def changing_size(hwnd):
            api.size = (1280, 720) if 1 <= self.clock.now < 1.2 or self.clock.now >= 3 else (1920, 1080)
            return read_size(hwnd)

        api.client_size = changing_size
        minimized_at = []
        minimize = api.minimize

        def record_minimize(hwnd):
            minimized_at.append(self.clock.now)
            minimize(hwnd)

        api.minimize = record_minimize
        self.prepare(api, minimize=True)
        self.assertEqual(len(minimized_at), 1)
        self.assertGreaterEqual(minimized_at[0], 3 + pc.GEOMETRY_STABLE_WINDOW)
        self.assertEqual(api.calls.count(('resize', (1280, 720))), 1)

    def test_maximized_window_restored_once_before_size_and_minimize(self):
        api = FakeAPI(size=(1920, 1080), maximized=True)
        self.prepare(api, minimize=True)
        self.assertFalse(api.zoomed)
        self.assertEqual(api.calls.count(('restore',)), 1)
        self.assertLess(api.calls.index(('restore',)), api.calls.index(('resize', (1280, 720))))
        self.assertLess(api.calls.index(('resize', (1280, 720))), api.calls.index(('minimize',)))

    def test_fullscreen_not_confirmed_never_minimizes(self):
        api = FakeAPI(size=(1280, 720), fullscreen=True)
        api.exit_ok = False
        self.assertIsNotNone(self.prepare(api, minimize=True))
        self.assertNotIn(('minimize',), api.calls)
        self.assertIn('本次不再最小化', '\n'.join(self.warnings))

    def test_cancellation_during_delayed_geometry_does_not_minimize(self):
        api = FakeAPI(size=(1920, 1080))
        api.resize_ok = False
        self.budget.cancelled = lambda: self.clock.now >= 1
        with self.assertRaises(common.Cancelled):
            self.prepare(api, minimize=True)
        self.assertNotIn(('minimize',), api.calls)

    def test_iconic_size_is_measured_after_restore(self):
        api = FakeAPI(size=(1280, 720), minimized=True)
        self.prepare(api, minimize=True)
        self.assertIn(('read', (0, 0)), api.calls)
        self.assertIn('1280×720 → 1280×720（无需调整', self.logs[-1])
        self.assertNotIn('0×0 →', self.logs[-1])

    def test_restore_can_finish_after_the_old_five_probe_limit(self):
        api = FakeAPI(size=(1280, 720), minimized=True)
        api.restore_ok = False
        def delayed_restore(hwnd):
            if self.clock.now >= 2:
                api.iconic = False
            return api.iconic

        api.minimized = delayed_restore
        self.prepare(api)
        self.assertEqual(api.calls.count(('restore',)), 1)
        self.assertGreaterEqual(self.clock.now, 2)
        self.assertLess(self.clock.now, 5.1)

    def test_minimize_failure_is_only_a_hint(self):
        # 19e4edc0 把最小化确认降级成提示，但漏更新了这个文件的断言（本次一并修）。
        api = FakeAPI(size=(1280, 720))
        api.minimize_ok = False
        self.assertIsNotNone(self.prepare(api, minimize=True))
        self.assertLessEqual(self.clock.now, pc.MINIMIZE_WINDOW + pc.GEOMETRY_STABLE_WINDOW + 0.2)
        self.assertEqual(api.calls.count(('minimize',)), 1)
        self.assertIn('最小化未确认', self.logs[-1])
        # 文案要说清请求已经发出，因为窗口很可能稍后自行最小化。
        self.assertIn('请求已发出', self.logs[-1])

    def test_minimize_is_requested_exactly_once_and_a_late_effect_counts(self):
        # 绝不重发。残留的请求会在框架做过伪最小化之后才被消化，让窗口再次 iconic，
        # Unity 恢复时抢回前台，于是框架的撤销条件
        # （pseudo_minimized_ && GetForegroundWindow() == hwnd_）成立，
        # 它把自己刚设的透明撤掉——实机表现为「最小化后又弹回前台」。
        api = FakeAPI(size=(1280, 720))
        api.minimize_ok = False  # 调用本身不改状态，改由下面的时间函数决定何时生效
        api.pseudo_minimized = lambda hwnd: self.clock.now >= 3.0
        self.assertIsNotNone(self.prepare(api, minimize=True))
        self.assertEqual(api.calls.count(('minimize',)), 1)
        self.assertGreaterEqual(self.clock.now, 3.0)
        self.assertIn('最小化状态已确认', self.logs[-1])

    def test_short_side_below_720_warns_about_precision(self):
        # 实机遇到过 1006×565：长宽比对得上，但短边不足 720，框架得放大画面来识别。
        api = FakeAPI(size=(1006, 565))
        api.resize_ok = False
        self.assertIsNotNone(self.prepare(api))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('短边只有 565 像素', self.warnings[0])
        self.assertIn('精度可能下降', self.warnings[0])
        self.assertNotIn('不影响准确度', self.warnings[0])

    def test_pseudo_minimized_correct_size_not_minimized_again(self):
        api = FakeAPI(size=(1280, 720), pseudo=True)
        self.prepare(api, minimize=True)
        self.assertNotIn(('minimize',), api.calls)
        self.assertFalse(any(c[0] == 'resize' for c in api.calls))
        self.assertIn('无需调整', self.logs[-1])

    def test_pseudo_minimized_is_not_an_error_when_minimize_is_off(self):
        # 伪最小化是框架自己的后台截图机制。把它当故障曾让整个任务队列全灭。
        api = FakeAPI(size=(1280, 720), pseudo=True)
        self.assertIsNotNone(self.prepare(api, minimize=False))
        self.assertTrue(api.pseudo)
        self.assertIn('后台截图模式', self.logs[-1])
        self.assertEqual(self.warnings, [])

    # ---- pretask 路径：只拉起并确认存在 ----

    def test_pretask_never_touches_the_window(self):
        api = FakeAPI()
        api.scan = Mock(return_value=([0x123], True, []))
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertEqual(api.calls, [])
        self.assertEqual(api.size, (800, 600))
        self.assertIn('已确认游戏主窗口', self.logs[-1])

    def test_pretask_leaves_minimized_window_alone(self):
        # 被手动最小化的窗口原样交给软件连接，框架首次截图时自会做伪最小化。
        api = FakeAPI(size=(1280, 720), minimized=True)
        api.scan = Mock(return_value=([0x123], True, []))
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertTrue(api.iconic)
        self.assertEqual(api.calls, [])

    def test_pretask_accepts_multiple_windows(self):
        # 多窗口交给软件自己的窗口选择，pretask 没资格替用户决定。
        api = FakeAPI()
        api.scan = Mock(return_value=([0x123, 0x456], True, []))
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertEqual(api.calls, [])
        self.assertIn('2 个', self.logs[-1])

    def test_pretask_launches_once_then_waits(self):
        frames = [([], False, []), ([], True, []), ([0x123], True, [])]
        api = FakeAPI()
        api.scan = Mock(side_effect=frames)
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertEqual(api.calls, [('launch',)])

    def test_pretask_settles_a_freshly_launched_window(self):
        # 亲手拉起的窗口刚出现时游戏界面线程忙于加载，直接交出去会让 sink 的
        # resize 与最小化全部落空。这段等待放在 pretask（独立进程）里不阻塞 pipeline。
        api = FakeAPI()
        frames = iter([([], False, []), ([0x123], True, [])])
        api.scan = lambda: next(frames, ([0x123], True, []))
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertGreaterEqual(self.clock.now, pc.SETTLE_AFTER_LAUNCH)
        self.assertEqual(api.calls.count(('launch',)), 1)
        self.assertIn('窗口编号已经变了', self.logs[-1])
        self.assertIn('刷新连接目标', self.logs[-1])

    def test_pretask_does_not_settle_a_game_that_was_already_running(self):
        api = FakeAPI()
        api.scan = Mock(return_value=([0x123], True, []))
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertEqual(self.clock.now, 0)
        self.assertEqual(api.calls, [])
        self.assertNotIn('窗口编号', self.logs[-1])

    def test_settle_detects_a_window_that_vanishes_before_it_is_ready(self):
        # 启动失败会闪退。把一个已失效的句柄交给 sink 只会换来「窗口已失效」。
        api = FakeAPI()
        frames = iter([([], False, []), ([0x123], True, [])])
        api.scan = lambda: next(frames, ([0x123], True, []))
        alive = iter([True, False])
        api.is_game = lambda hwnd: next(alive, True)
        self.assertIsNotNone(pc.prepare(api, self.budget))
        self.assertEqual(api.calls.count(('launch',)), 1)
        self.assertTrue(any('在就绪前消失' in text for text in self.logs))

    def test_sink_applies_minimize_after_pretask_and_connection(self):
        api = FakeAPI()
        api.scan = Mock(return_value=([0x123], True, []))
        pc.prepare(api, self.budget)
        self.assertEqual(api.calls, [])
        pc.prepare(api, self.budget, hwnd=0x123, options=options.PCOptions('1080p', True))
        self.assertTrue(api.pseudo)
        self.assertEqual(api.calls.count(('minimize',)), 1)
        self.assertIn('[sink]', self.logs[-1])
        self.assertIn('最小化', self.logs[-1])

    # ---- guard 的失败收场 ----

    def test_guard_never_calls_post_inactive(self):
        # post_inactive 的语义是取消置顶 + 解除输入阻断，不碰 layered/alpha，
        # 拿它「救援」伪最小化只会空转然后二次报错。别再加回来。
        api = FakeAPI(size=(1280, 720), pseudo=True)
        fake_module = types.ModuleType('startup.win32')
        fake_module.WindowsAPI = lambda: api
        controller = context().tasker.controller
        controller.post_inactive = Mock(side_effect=AssertionError('post_inactive 不该再被调用'))
        with patch.dict(sys.modules, {'startup.win32': fake_module}):
            result = guard.StartupGuard._pc_prepare(controller, controller.info, self.budget,
                                                    options.PCOptions(minimize=False))
        self.assertIsNotNone(result)
        controller.post_inactive.assert_not_called()
        self.assertTrue(api.pseudo)
        self.assertIn('后台截图模式', self.logs[-1])

    def test_failure_is_remembered_per_task_and_retried_next_task(self):
        prepare = Mock(side_effect=common.PreparationError('窗口没了'))
        errors = []
        g = guard.StartupGuard(self.logs.append, errors.append, pc_prepare=prepare,
                               budget_factory=self.budget_factory)
        ctx = context()
        self.assertIsNone(g.ensure(ctx, 1))
        # 同一任务内的后续节点命中失败缓存：不重跑准备、不重发 post_stop、不刷第二条错误。
        self.assertIsNone(g.ensure(ctx, 1))
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(ctx.tasker.post_stop.call_count, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn('下个任务会重新检查', errors[0])
        self.assertNotIn('重新启动软件', errors[0])
        # PC 不再有会话级失败表，下个任务重新检查。
        self.assertEqual(g.failures, {})
        self.assertIsNone(g.ensure(ctx, 2))
        self.assertEqual(prepare.call_count, 2)

    def test_adb_keeps_session_level_latch(self):
        # ADB 预算是 300 秒，去掉记忆会让模拟器掉线时每个任务空等一轮，比现状更糟。
        adb_prepare = Mock(side_effect=common.PreparationError('设备掉线'))
        errors = []
        g = guard.StartupGuard(self.logs.append, errors.append, adb_prepare=adb_prepare,
                               budget_factory=self.budget_factory)
        ctx = context('adb')
        self.assertIsNone(g.ensure(ctx, 1))
        self.assertIsNone(g.ensure(ctx, 2))
        self.assertEqual(adb_prepare.call_count, 1)
        self.assertEqual(g.failures, {('adb', 'fake'): '设备掉线'})
        self.assertIn('重新启动软件', errors[-1])

    def test_pc_gets_short_budget_while_adb_keeps_default(self):
        seen = []

        def factory(report, cancelled, **kwargs):
            seen.append(kwargs.get('timeout'))
            return self.budget_factory(report, cancelled, **kwargs)

        g = guard.StartupGuard(self.logs.append, self.fail, budget_factory=factory,
                               adb_prepare=Mock(return_value=common.Prepared()),
                               pc_prepare=Mock(return_value=common.Prepared()))
        g.ensure(context('win32'), 1)
        g.ensure(context('adb'), 2)
        self.assertEqual(seen, [pc.PC_TASK_TIMEOUT, None])

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
        for kind in ('adb', 'custom', 'native_android'):
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
