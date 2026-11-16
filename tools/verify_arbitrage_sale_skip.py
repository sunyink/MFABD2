"""④ 卖料理前用库存记录跳过确定没货的柜台（U62）：只认这一段里读到的 0。

不连接游戏、不加载资源；出售主控只跑到派发为止，派发本身被替换。
运行：python -B tools/verify_arbitrage_sale_skip.py -v
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from maa.library import Library

Library.version()
from maa.agent.agent_server import AgentServer

with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
     patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
     patch.object(AgentServer, "context_sink", return_value=lambda cls: cls), \
     patch.object(AgentServer, "tasker_sink", return_value=lambda cls: cls), \
     patch.object(AgentServer, "_set_api_properties"):
    from action import arbitrage_result as controller

from utils import arbitrage_sale_skip as skip

T0 = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def scan(minutes, session="s1", **origin):
    return {"kind": "inventory_scan", "started_at": at(minutes),
            "origin": {"mark": "open", "type": "shop", "session": session, **origin}}


def zero(minutes, source="bag_absent"):
    return {"quantity": 0, "quantity_status": "known", "quantity_observed_at": at(minutes),
            "quantity_source": source}


RUNNING = [{"entry": "Arbitrage_Start", "start": T0.timestamp(), "end": None}]


class TrustSinceTests(unittest.TestCase):
    def test_without_a_running_task_no_zero_is_trusted(self):
        finished = [dict(RUNNING[0], end=T0.timestamp() + 60)]
        self.assertIsNone(skip.trust_since([scan(5)], finished, "s1"))
        self.assertIsNone(skip.trust_since([scan(5)], [], "s1"))

    def test_latest_ledger_scan_of_this_session_is_the_floor(self):
        events = [scan(-30), scan(5), scan(9, retry_of="x"), scan(20, session="old"),
                  {"kind": "inventory_scan", "started_at": at(30)},          # 不是记账盘的（比如料理后补查）
                  {"kind": "inventory_set", "observed_at": at(40)}]
        self.assertEqual(skip.trust_since(events, RUNNING, "s1"), T0 + timedelta(minutes=5))

    def test_without_ledger_scans_only_this_task_counts(self):
        self.assertEqual(skip.trust_since([scan(5, session="old")], RUNNING, "s1"), T0)


class KnownEmptyTests(unittest.TestCase):
    def test_only_fresh_known_zero(self):
        items = {
            "鸡肉卷": zero(6),
            "蘑菇汤": zero(4),                                  # 盘点之前读到的
            "炒蘑菇": {"quantity_status": "unknown"},
            "草药三明治": dict(zero(6), quantity=3),
            "烤蜂蜜苹果": dict(zero(6), quantity_observed_at="2026-11-15T10:06:00"),  # 没时区，不猜
            "香甜马卡龙": zero(7, source="sale_confirmed"),
        }
        names = ["鸡肉卷", "蘑菇汤", "炒蘑菇", "草药三明治", "烤蜂蜜苹果", "雞肉捲", "香甜马卡龙", "没记过的"]
        result = skip.known_empty(names, items, T0 + timedelta(minutes=5))
        self.assertEqual(set(result), {"鸡肉卷", "雞肉捲", "香甜马卡龙"})
        self.assertEqual(result["鸡肉卷"][0], "盘点判 0")
        self.assertEqual(result["香甜马卡龙"][0], "卖光了")
        self.assertEqual(skip.known_empty(names, items, None), {})


def peak(name, cart):
    return {"name": name, "is_max_price": True, "target_cartridge": cart, "cart_score": 1.0,
            "cart_conflict": False, "current_rate": 130, "cartridge_read_basis": "direct"}


MARKET = {"complete": True, "items": [peak("鸡肉卷", "剧情游戏卡6"), peak("蘑菇汤", "剧情游戏卡5")]}
SOLD = {"status": "confirmed", "actual_quantity": 4, "batches": [{"status": "confirmed"}], "page_ok": True}


class SellDispatchTests(unittest.TestCase):
    def run_sell(self, items, since=T0, params=None, extra=()):
        ui = SimpleNamespace(tasker=SimpleNamespace(stopping=False),
                             get_node_object=lambda name: SimpleNamespace(attach={}))
        logs, sent = [], []
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(controller, "market_day", return_value="2026-11-15"))
            mocks.enter_context(patch.object(controller, "get_market_snapshot", return_value=MARKET))
            mocks.enter_context(patch.object(controller, "_load_recipe_names", return_value={"鸡肉卷", "蘑菇汤"}))
            mocks.enter_context(patch.object(controller, "get_inventory_items", return_value=items))
            if isinstance(since, Exception):
                mocks.enter_context(patch.object(controller, "trust_since", side_effect=since))
            else:
                mocks.enter_context(patch.object(controller, "trust_since", return_value=since))
            mocks.enter_context(patch.object(
                controller, "execute_sale_item",
                side_effect=lambda ctx, name, cfg, reserve=0: sent.append(name) or SOLD))
            mocks.enter_context(patch.object(controller.mfaalog, "info", side_effect=logs.append))
            mocks.enter_context(patch.object(controller.mfaalog, "warning", side_effect=logs.append))
            for patcher in extra:
                mocks.enter_context(patcher)
            ok = controller.ArbitrageSellController()._run(ui, params or {"mode": "sell"})
        return ok, sent, logs

    def test_known_zero_dish_is_not_visited(self):
        ok, sent, logs = self.run_sell({"鸡肉卷": zero(1), "蘑菇汤": {"quantity_status": "unknown"}})
        self.assertTrue(ok)
        self.assertEqual(sent, ["蘑菇汤"])
        self.assertTrue(any(line.startswith("[Arbitrage] [鸡肉卷] 库存记录为 0（盘点判 0，") and
                            line.endswith("），不去柜台") for line in logs))
        self.assertIn("[Arbitrage] 🛒 确认共 1 项物品待出售: 蘑菇汤", logs)
        self.assertEqual(logs[-1], "[Arbitrage] 本轮实际售出 1 项：蘑菇汤，1项库存记录为0、没去柜台")

    def test_all_known_zero_means_no_trip(self):
        ok, sent, logs = self.run_sell({"鸡肉卷": zero(1), "蘑菇汤": zero(2, "sale_confirmed")})
        self.assertTrue(ok)
        self.assertEqual(sent, [])
        self.assertEqual(logs[-1], "[Arbitrage] 💤 今日峰值料理库存都记录为 0，不去柜台：鸡肉卷, 蘑菇汤")

    def test_stale_zero_or_unreadable_records_still_go(self):
        for since in (T0 + timedelta(minutes=5), None, RuntimeError("存档坏了")):
            with self.subTest(since=since):
                ok, sent, logs = self.run_sell({"鸡肉卷": zero(1), "蘑菇汤": zero(2)}, since=since)
                self.assertTrue(ok)
                self.assertEqual(sent, ["鸡肉卷", "蘑菇汤"])
                self.assertFalse(any("没去柜台" in line for line in logs))
        self.assertIn("[Arbitrage] 库存记录读不到(存档坏了)，照旧逐家柜台核对", logs)

    def test_material_sale_is_not_filtered(self):
        policy = SimpleNamespace(sale_eligible={"鸡肉卷", "蘑菇汤"}, reserve_for=lambda name: 0,
                                 describe=lambda: "测试保留")
        ok, sent, _ = self.run_sell(
            {"鸡肉卷": zero(1), "蘑菇汤": zero(2)}, params={"mode": "sell", "sale_scope": "materials"},
            extra=[patch.object(controller, "read_material_reserve_policy", return_value=policy),
                   patch.object(controller, "_load_recipe_names", return_value=set())])
        self.assertTrue(ok)
        self.assertEqual(sent, ["鸡肉卷", "蘑菇汤"])


if __name__ == "__main__":
    unittest.main()
