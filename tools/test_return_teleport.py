"""Regression tests for return-teleport catalog, card selection, and batch safety."""

import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steal_test_helpers import BASE, ROOT, RecognitionDetail


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.01, seconds)


def _bright_frame(value=180):
    return np.full((720, 1280, 3), value, dtype=np.uint8)


def _dark_frame():
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _fake_frame(tag):
    return SimpleNamespace(tag=tag, size=720 * 1280 * 3, shape=(720, 1280, 3))


class _ScriptedCardContext:
    def __init__(self, frames, hits_fn):
        self._frames = list(frames)
        self._index = 0
        self._hits_fn = hits_fn
        self.actions = []
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=SimpleNamespace(
                info={"type": "win32"},
                connected=True,
                post_screencap=self._screencap,
            ),
        )

    def _screencap(self):
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return SimpleNamespace(done=True, succeeded=True, get=lambda f=frame: f)

    def clone(self):
        return self

    def run_recognition(self, name, image, overrides=None):
        suffix = name.removeprefix("ReturnCards_")
        hit, box = self._hits_fn(suffix, image)
        return SimpleNamespace(hit=bool(hit), box=box if hit else None)

    def run_action(self, name, pipeline_override=None):
        self.actions.append((name, pipeline_override))
        return SimpleNamespace(success=True)


class ReturnTeleportCatalogAndCardsTests(unittest.TestCase):
    def setUp(self):
        package_name = "_return_tp_test_pkg"
        package = ModuleType(package_name)
        package.__path__ = [str(ROOT / "agent/action")]
        stub_modules = {key: ModuleType(key) for key in (
            "maa", "maa.agent", "maa.agent.agent_server", "maa.custom_action", "maa.define",
            "utils", "utils.account_sync",
        )}
        stub_modules[package_name] = package
        stub_modules["maa.agent.agent_server"].AgentServer = SimpleNamespace(custom_action=lambda _: lambda cls: cls)
        stub_modules["maa.custom_action"].CustomAction = type("CustomAction", (), {})
        stub_modules["maa.define"].RecognitionDetail = RecognitionDetail
        stub_modules["utils"].mfaalog = SimpleNamespace(**{level: Mock() for level in ("info", "error", "debug", "warning")})
        stub_modules["utils.account_sync"].sync_from_context = Mock(return_value=True)
        self._patcher = patch.dict(sys.modules, stub_modules)
        self._patcher.start()
        self.addCleanup(self._cleanup_modules, package_name)
        self.catalog_mod = importlib.import_module(package_name + ".return_teleport_catalog")
        self.cards_mod = importlib.import_module(package_name + ".return_teleport_cards")
        self.tp_mod = importlib.import_module(package_name + ".return_teleport")
        self.clock = _FakeClock()
        self._orig_cards_time = self.cards_mod.time
        self.cards_mod.time = self.clock
        self.addCleanup(setattr, self.cards_mod, "time", self._orig_cards_time)

    def _cleanup_modules(self, package_name):
        for key in list(sys.modules):
            if key == package_name or key.startswith(package_name + "."):
                del sys.modules[key]
        self._patcher.stop()

    def test_catalog_loads_both_json_and_jsonc_pipelines_with_comments(self):
        with tempfile.TemporaryDirectory(prefix="return_tp_catalog_") as tmp:
            root = Path(tmp)
            (root / "base" / "pipeline").mkdir(parents=True)
            (root / "base" / "image" / "Cartridges").mkdir(parents=True)
            (root / "pc" / "pipeline").mkdir(parents=True)
            (root / "pc" / "image" / "Cartridges").mkdir(parents=True)
            for num in (1, 2):
                (root / "base" / "image" / "Cartridges" / f"Q_sp{num}_PC.png").write_bytes(b"png")
            (root / "base" / "pipeline" / "cards.json").write_text(
                json.dumps({"Collect_Pack_Story_1": {"template": ["Cartridges/Q_sp1_PC.png"]}}),
                encoding="utf-8",
            )
            (root / "pc" / "pipeline" / "extra.jsonc").write_text(
                """{
                    // story card 2 in jsonc
                    "Collect_Pack_Story_2": {
                        /* comment with // inside */
                        "note": "keep // inside string",
                        "template": [
                            "Cartridges/Q_sp2_PC.png",
                        ],
                    },
                }""",
                encoding="utf-8",
            )
            catalog = self.catalog_mod.load_catalog(root)
            self.assertEqual([(c.category, c.number) for c in catalog], [("story", 1), ("story", 2)])
            self.assertEqual(catalog[1].templates, ("Cartridges/Q_sp2_PC.png",))

    def test_return_cards_primitives_declare_zero_timing_fields(self):
        nodes = json.loads((BASE / "pipeline/Return_Cards.json").read_text(encoding="utf-8"))
        for name in (
            "ReturnCards_Click",
            "ReturnCards_OpenQuickPanel",
            "ReturnCards_MenuBack",
            "ReturnCards_ScrollToStart",
            "ReturnCards_ScrollForward",
            "ReturnCards_TabStart",
            "ReturnCards_TabForward",
        ):
            with self.subTest(node=name):
                self.assertEqual(nodes[name].get("rate_limit"), 0)
                self.assertEqual(nodes[name].get("pre_delay"), 0)
                self.assertEqual(nodes[name].get("post_delay"), 0)

    def test_failure_polls_until_locked_dialog_actually_closes(self):
        locked_frame = _fake_frame("locked")
        still_locked = _fake_frame("still_locked")
        panel_frame = _fake_frame("panel")

        def hits(suffix, image):
            if image in (locked_frame, still_locked):
                if suffix in ("Locked", "Cancel"):
                    return True, [500, 400, 80, 30]
                return False, None
            return suffix == "QuickPanel", [10, 10, 20, 20]

        ctx = _ScriptedCardContext([locked_frame, still_locked, panel_frame], hits)
        nav = self.cards_mod.ReturnCardNavigator(ctx)
        with self.assertRaises(self.cards_mod.CardUnavailable) as cm:
            nav._failure(locked_frame)
        self.assertEqual(cm.exception.kind, "locked")
        self.assertEqual(len(ctx.actions), 1)

    def test_unclosed_rejection_dialog_stops_batch_preserving_card_rejection(self):
        locked_frame = _fake_frame("locked")

        def hits(suffix, image):
            if suffix in ("Locked", "Cancel"):
                return True, [500, 400, 80, 30]
            return False, None

        ctx = _ScriptedCardContext([locked_frame] * 20, hits)
        nav = self.cards_mod.ReturnCardNavigator(ctx)
        with self.assertRaises(self.cards_mod.CardRejectionUnclosed) as cm:
            nav._failure(locked_frame)
        self.assertEqual(cm.exception.kind, "locked")

        card1 = self.catalog_mod.ReturnCard("story", 1, ("Cartridges/Q_sp1_PC.png",), "Collect_Pack_Story_1")
        card2 = self.catalog_mod.ReturnCard("story", 2, ("Cartridges/Q_sp2_PC.png",), "Collect_Pack_Story_2")
        nav_mock = Mock()
        nav_mock.select_card.side_effect = self.cards_mod.CardRejectionUnclosed("locked", "弹窗未关闭")
        runner_factory = Mock()
        ok, outcomes = self.tp_mod.restore_cards(
            ctx, (card1, card2), navigator_factory=lambda _: nav_mock, runner_factory=runner_factory
        )
        self.assertFalse(ok)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "locked")
        runner_factory.assert_not_called()

    def test_wait_for_card_entry_require_leave_rejects_stale_field_and_accepts_transition(self):
        bright = _bright_frame()
        ctx_stale = _ScriptedCardContext([bright] * 40, lambda suf, img: (suf == "Field", [1, 1, 2, 2]))
        nav_stale = self.cards_mod.ReturnCardNavigator(ctx_stale)
        with self.assertRaisesRegex(self.cards_mod.CardUnavailable, "未离开原场景"):
            nav_stale.wait_for_card_entry(require_leave=True)

        dark = _dark_frame()
        ctx_ok = _ScriptedCardContext(
            [bright, dark, dark, bright, bright],
            lambda suf, img: (suf == "Field" and img is bright, [1, 1, 2, 2]),
        )
        nav_ok = self.cards_mod.ReturnCardNavigator(ctx_ok)
        result = nav_ok.wait_for_card_entry(require_leave=True)
        self.assertIs(result, bright)

    def test_restore_cards_passes_require_leave_only_for_subsequent_different_cards(self):
        card1 = self.catalog_mod.ReturnCard("story", 1, ("Cartridges/Q_sp1_PC.png",), "Collect_Pack_Story_1")
        card2 = self.catalog_mod.ReturnCard("story", 2, ("Cartridges/Q_sp2_PC.png",), "Collect_Pack_Story_2")
        seen_calls = []

        class FakeNav:
            def select_card(self, card, *, require_leave=False):
                seen_calls.append((card.number, require_leave))

        class FakeRunner:
            result = "already"

            def __init__(self, ctx, skip_if_at_portal=True):
                pass

            def run(self):
                return True

        ctx = SimpleNamespace(tasker=SimpleNamespace(stopping=False), clone=lambda: ctx)
        ok, outcomes = self.tp_mod.restore_cards(
            ctx, (card1, card2), navigator_factory=lambda _: FakeNav(), runner_factory=FakeRunner
        )
        self.assertTrue(ok)
        self.assertEqual(seen_calls, [(1, False), (2, True)])
        self.assertEqual([o["status"] for o in outcomes], ["already", "already"])

    def test_open_quick_panel_requires_two_stable_non_loading_frames(self):
        f_flicker = _bright_frame(170)
        f_loading = _dark_frame()
        f_stable1 = _bright_frame(180)
        f_stable2 = _bright_frame(185)
        f_panel = _bright_frame(190)

        def hits(suffix, img):
            if img is f_panel:
                return suffix == "QuickPanel", [10, 10, 20, 20]
            if img is f_loading:
                return False, None
            if suffix in ("Field", "QuickButton"):
                return True, [550, 650, 30, 30]
            return False, None

        ctx = _ScriptedCardContext([f_flicker, f_loading, f_stable1, f_stable2, f_stable2, f_panel], hits)
        nav = self.cards_mod.ReturnCardNavigator(ctx)
        result = nav.open_quick_panel()
        self.assertIs(result, f_panel)
        self.assertEqual(len(ctx.actions), 1)

    def test_select_card_rechecks_duplicate_signature_before_stopping(self):
        card = self.catalog_mod.ReturnCard("story", 5, ("Cartridges/Q_sp5_PC.png",), "Collect_Pack_Story_5")
        page0 = _bright_frame(100)
        page1_lag = _bright_frame(100)
        page1_settled = _bright_frame(200)
        arrived = _bright_frame(210)

        captures = iter([
            page0,
            page0, page0,
            page0,
            page0,
            page1_lag,
            page1_settled,
            page1_settled,
            arrived, arrived,
        ])

        def hits(suffix, img):
            if img is arrived:
                return suffix == "Field", [1, 1, 2, 2]
            if suffix in ("QuickPanel", "StorySelected"):
                return True, [10, 10, 20, 20]
            if suffix == "Card" and img is page1_settled:
                return True, [400, 620, 40, 40]
            return False, None

        ctx = _ScriptedCardContext([], hits)
        nav = self.cards_mod.ReturnCardNavigator(ctx)
        nav.capture = lambda: next(captures)
        nav.open_quick_panel = lambda: page0
        nav._select_category = lambda: page0
        nav.select_card(card)
        click_actions = [a for a in ctx.actions if a[0] == "ReturnCards_Click"]
        self.assertEqual(len(click_actions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
