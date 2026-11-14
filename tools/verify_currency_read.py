"""顶栏货币读数回归：数字解析、按图标切数字区、钻石弹窗的打开 / 展开 / 关闭，付费钻石沿用，读数事件存档。

全程合成画面与内存存档，不连接游戏、不读写真实存档。需要 maa 包（3.11 venv）。直接运行：
    python -B tools/verify_currency_read.py -v
"""

import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from maa.library import Library

Library.version()  # 在导入 maa.agent 前固定为本地 MaaFramework 模式。
from utils import arbitrage_store as store  # noqa: E402
from utils import inventory_store  # noqa: E402

spec = importlib.util.spec_from_file_location("currency_read", ROOT / "agent/action/currency_read.py")
currency = importlib.util.module_from_spec(spec)
with patch("maa.agent.agent_server.AgentServer.custom_action", return_value=lambda cls: cls):
    spec.loader.exec_module(currency)

ATTACH = {"tail_width": 110, "max_lead": 20, "half_height": 16}
# 模拟器箱庭实测：图标框（make_icons.py 的位置）
ICONS = {"diamond": [464, 39, 17, 15], "gold": [581, 37, 16, 19], "tonic": [724, 36, 18, 20]}
ICON_NODE = {node: name for name, node in currency.ICON_NODES}


def ocr(*pairs):
    return SimpleNamespace(hit=bool(pairs), box=None,
                           filtered_results=[SimpleNamespace(text=text, box=box) for text, box in pairs])


class FakeScreen:
    """假画面：bar（只有顶栏）/ popup（钻石弹窗）/ detail（展开免费 / 付费）。

    点击、重试、等弹窗停稳都在 pipeline 链里，这里只按 chain 模拟链跑完后停在哪：
    shown 走到「详细拥有量」；no_popup 点了图标没出弹窗；no_detail 弹窗开了但没展开。
    """

    def __init__(self, *, numbers=None, free="拥有137,227个", paid="拥有0个", chain="shown", state="bar"):
        self.state = state
        self.numbers = numbers if numbers is not None else {
            "diamond": [("137,227", [480, 32, 81, 26])],
            "gold": [("42,522,214", [600, 34, 106, 23])],
            "tonic": [("471,245", [745, 34, 79, 24])],
        }
        self.free, self.paid, self.chain = free, paid, chain
        self.tasks, self.cleared = [], []
        self.tasker = SimpleNamespace(
            stopping=False,
            controller=SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                wait=lambda: SimpleNamespace(get=lambda: np.zeros((720, 1280, 3), dtype=np.uint8)))),
        )

    def get_node_object(self, name):
        assert name == currency.NUMBER_NODE
        return SimpleNamespace(attach=ATTACH)

    def clear_hit_count(self, node):
        self.cleared.append(node)
        return True

    def run_task(self, entry, override=None):
        self.tasks.append(entry)
        names = []
        if entry == currency.DETAIL_ENTRY:
            names = [currency.DETAIL_ENTRY]
            if self.state == "bar":
                names.append(currency.OPEN_NODE)
                if self.chain == "no_popup":
                    names.append(currency.OPEN_NODE)
                else:
                    self.state = "popup"
            if self.state == "popup":
                names.append(currency.EXPAND_NODE)
                if self.chain == "no_detail":
                    names.append(currency.EXPAND_NODE)
                else:
                    self.state = "detail"
                    names.append(currency.SHOWN_NODE)
        elif entry == currency.CLOSE_NODE and self.state in ("popup", "detail"):
            self.state = "bar"
            names = [currency.CLOSE_NODE]
        return SimpleNamespace(nodes=[SimpleNamespace(name=name, completed=True) for name in names])

    def run_recognition(self, node, image, override=None):
        if node in ICON_NODE:
            box = ICONS.get(ICON_NODE[node])
            return SimpleNamespace(hit=box is not None, box=box)
        if node == currency.NUMBER_NODE:
            roi = override[node]["roi"]
            name = min(ICONS, key=lambda key: abs(ICONS[key][0] + ICONS[key][2] + 1 - roi[0]))
            return ocr(*self.numbers.get(name, []))
        if node == currency.TITLE_NODE:
            return SimpleNamespace(hit=self.state in ("popup", "detail"), box=[266, 193, 52, 32])
        if node == currency.FREE_NODE:
            return ocr((self.free, [438, 309, 121, 22])) if self.state == "detail" else ocr()
        if node == currency.PAID_NODE:
            return ocr((self.paid, [493, 331, 68, 26])) if self.state == "detail" else ocr()
        raise AssertionError(node)


class Fast(unittest.TestCase):
    def setUp(self):
        currency._PAID_BY_ACCOUNT.clear()
        patcher = patch.object(currency.PersistentStore, "_current_account_id", "7")
        patcher.start()
        self.addCleanup(patcher.stop)


class ParseTests(unittest.TestCase):
    def test_grouped_numbers_accept_comma_or_point(self):
        for text, value in (("137,227", 137227), ("42,522.214", 42522214), ("1,812,913", 1812913),
                            ("0", 0), ("999", 999), (" 179,001,139 ", 179001139)):
            self.assertEqual(currency.parse_grouped(text), value, text)

    def test_icon_merged_or_ungrouped_numbers_are_rejected(self):
        for text in ("6137,227", "137227", "42,522,2", "0,123", "+", ""):
            self.assertIsNone(currency.parse_grouped(text), text)

    def test_owned_line(self):
        self.assertEqual(currency.parse_owned("拥有137.227个"), 137227)
        self.assertEqual(currency.parse_owned("持有4,130個"), 4130)
        self.assertEqual(currency.parse_owned("拥有0个"), 0)
        self.assertIsNone(currency.parse_owned("持有283.509個+"))


class BarTests(Fast):
    def test_rois_run_to_next_icon_and_tail_for_the_last(self):
        rois = currency.number_rois(ICONS, ATTACH, 1280, 720)
        self.assertEqual(rois["diamond"], [482, 30, 581 - 2 - 482, 32])
        self.assertEqual(rois["gold"], [598, 30, 724 - 2 - 598, 32])
        self.assertEqual(rois["tonic"], [743, 30, 110, 32])

    def test_missing_gold_icon_lets_diamond_run_to_tonic(self):
        icons = dict(ICONS, gold=None)
        rois = currency.number_rois(icons, ATTACH, 1280, 720)
        self.assertNotIn("gold", rois)
        self.assertEqual(rois["diamond"][0] + rois["diamond"][2], 724 - 2)

    def test_widest_valid_number_near_the_icon_wins(self):
        screen = FakeScreen(numbers={"diamond": [("6", [482, 34, 8, 20]), ("137,227", [484, 32, 75, 26])],
                                     "gold": [("42,522,214", [600, 34, 106, 23])],
                                     "tonic": [("471,245", [745, 34, 79, 24]), ("1", [830, 30, 10, 10])]})
        values = currency.read_bar(screen, np.zeros((720, 1280, 3)))["values"]
        self.assertEqual(values, {"diamond": 137227, "gold": 42522214, "tonic": 471245})

    def test_text_far_from_the_icon_is_not_taken(self):
        screen = FakeScreen(numbers={"tonic": [("12", [800, 30, 30, 20])]})
        self.assertIsNone(currency.read_bar(screen, np.zeros((720, 1280, 3)))["values"]["tonic"])


class DiamondTests(Fast):
    def test_detail_read_runs_chain_reads_and_closes(self):
        screen = FakeScreen()
        record = currency.read_currency(screen, diamond_detail=True)
        self.assertEqual((record["diamond"], record["diamond_free"], record["diamond_paid"], record["diamond_paid_source"]),
                         (137227, 137227, 0, "read"))
        self.assertEqual(screen.tasks, [currency.DETAIL_ENTRY, currency.CLOSE_NODE])
        self.assertEqual((screen.state, record["detail_closed"], record["detail_error"]), ("bar", True, None))

    def test_chain_hit_counts_are_cleared_before_reading_and_before_the_chain(self):
        screen = FakeScreen()
        currency.read_currency(screen, diamond_detail=True)
        self.assertEqual(screen.cleared[:len(currency.CHAIN_NODES)], list(currency.CHAIN_NODES))
        self.assertGreaterEqual(screen.cleared.count(currency.OPEN_NODE), 2)
        self.assertEqual(screen.cleared[-1], currency.CLOSE_NODE)

    def test_later_reads_carry_paid_and_subtract(self):
        currency.read_currency(FakeScreen(free="拥有133,097个", paid="拥有4,130个"), diamond_detail=True)
        screen = FakeScreen(numbers={"diamond": [("140,000", [480, 32, 81, 26])]})
        record = currency.read_currency(screen)
        self.assertEqual(screen.tasks, [])
        self.assertEqual((record["diamond_free"], record["diamond_paid"], record["diamond_paid_source"]),
                         (135870, 4130, "carried"))

    def test_auto_reads_detail_once_per_account(self):
        first = FakeScreen()
        currency.read_currency(first)
        self.assertIn(currency.DETAIL_ENTRY, first.tasks)
        with patch.object(currency.PersistentStore, "_current_account_id", "8"):
            other = FakeScreen()
            currency.read_currency(other)
            self.assertIn(currency.DETAIL_ENTRY, other.tasks)

    def test_sum_mismatch_is_not_trusted_or_cached(self):
        record = currency.read_currency(FakeScreen(free="拥有100个", paid="拥有5个"), diamond_detail=True)
        self.assertEqual((record["diamond_paid"], record["detail_error"]), (None, "detail_sum_mismatch"))
        self.assertEqual(currency._PAID_BY_ACCOUNT, {})

    def test_popup_not_opening_reads_nothing_and_close_finds_nothing(self):
        screen = FakeScreen(chain="no_popup")
        record = currency.read_currency(screen, diamond_detail=True)
        self.assertEqual((record["detail_error"], record["detail_closed"], record["diamond"], screen.state),
                         ("popup_not_open", True, 137227, "bar"))

    def test_popup_without_detail_is_closed(self):
        screen = FakeScreen(chain="no_detail")
        record = currency.read_currency(screen, diamond_detail=True)
        self.assertEqual((record["detail_error"], record["detail_closed"], screen.state),
                         ("detail_not_shown", True, "bar"))

    def test_no_detail_when_disabled(self):
        screen = FakeScreen()
        record = currency.read_currency(screen, diamond_detail=False)
        self.assertEqual((screen.tasks, record["diamond_paid"]), ([], None))


class MemoryAccountStore:
    data = {}
    _current_account_id = "7"

    @classmethod
    def load(cls):
        return copy.deepcopy(cls.data)

    @classmethod
    def save(cls, data):
        cls.data = copy.deepcopy(data)
        return True


class StoreTests(unittest.TestCase):
    def setUp(self):
        MemoryAccountStore.data = {}
        patcher = patch.object(store, "PersistentStore", MemoryAccountStore)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reading_goes_to_events_and_latest_without_touching_items(self):
        record = {"label": "测试", "observed_at": "2026-11-13T00:00:00+00:00", "gold": 5, "diamond": None,
                  "tonic": 7, "diamond_free": None, "diamond_paid": None, "diamond_paid_source": None,
                  "icons_found": ["gold", "tonic"], "detail_error": None, "detail_closed": None, "extra": 1}
        self.assertTrue(inventory_store.save_currency_reading(record))
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        event = inventory["events"][-1]
        self.assertEqual((event["kind"], event["label"], event["gold"], event["diamond"]), ("currency_reading", "测试", 5, None))
        self.assertNotIn("extra", event)
        self.assertEqual(inventory["latest"]["currency"]["tonic"], 7)
        self.assertEqual(inventory["items"], {})


class ActionTests(Fast):
    def test_action_saves_with_label_and_never_fails_the_flow(self):
        screen = FakeScreen()
        argv = SimpleNamespace(custom_action_param={"label": "开期", "diamond_detail": True})
        with patch.object(currency, "sync_from_context", return_value=True), \
                patch.object(currency, "save_currency_reading", return_value=True) as save:
            self.assertTrue(currency.CurrencyRead().run(screen, argv))
        self.assertEqual(save.call_args[0][0]["label"], "开期")
        with patch.object(currency, "sync_from_context", side_effect=RuntimeError("boom")):
            self.assertTrue(currency.CurrencyRead().run(screen, argv))

    def test_missing_label_or_account_reads_nothing(self):
        screen = FakeScreen()
        with patch.object(currency, "save_currency_reading") as save:
            self.assertTrue(currency.CurrencyRead().run(screen, SimpleNamespace(custom_action_param={})))
            with patch.object(currency, "sync_from_context", return_value=False):
                self.assertTrue(currency.CurrencyRead().run(
                    screen, SimpleNamespace(custom_action_param={"label": "x"})))
        save.assert_not_called()
        self.assertEqual(screen.tasks, [])


if __name__ == "__main__":
    unittest.main()
