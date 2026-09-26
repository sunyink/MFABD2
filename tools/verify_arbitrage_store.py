"""商店套利存档结构回归检查；全程内存替身，不读写用户真实存档。"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from utils import arbitrage_store as store
from utils.persistent_store import PersistentStore as RealAccountStore
from utils.persistent_store import SharedStore as RealSharedStore


class MemoryAccountStore:
    data = {}

    @classmethod
    def load(cls):
        return copy.deepcopy(cls.data)

    @classmethod
    def save(cls, data):
        cls.data = copy.deepcopy(data)
        return True


class MemorySharedStore(MemoryAccountStore):
    data = {}


class ArbitrageStoreTests(unittest.TestCase):
    def setUp(self):
        MemoryAccountStore.data = {}
        MemorySharedStore.data = {}
        self.account_patch = patch.object(store, "PersistentStore", MemoryAccountStore)
        self.shared_patch = patch.object(store, "SharedStore", MemorySharedStore)
        self.account_patch.start()
        self.shared_patch.start()

    def tearDown(self):
        self.shared_patch.stop()
        self.account_patch.stop()

    @staticmethod
    def scan(complete=True, name="烤蜂蜜苹果"):
        return {
            "observed_at": "2026-09-08T01:02:03+00:00",
            "complete": complete,
            "termination_reason": "non_max_boundary" if complete else "parse_empty",
            "pages_scanned": 2,
            "items": [{
                "name": name,
                "is_max_price": True,
                "target_cartridge": "剧情游戏卡17",
                "cart_score": 0.99,
            }],
        }

    def test_market_cache_only_accepts_complete_and_is_not_downgraded(self):
        day = "2026-09-08"
        self.assertTrue(store.save_market_snapshot(self.scan(False), day))
        self.assertIsNone(store.get_market_snapshot(day))
        self.assertTrue(store.save_market_snapshot(self.scan(True), day))
        complete = store.get_market_snapshot(day)
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["items"][0]["name"], "烤蜂蜜苹果")
        self.assertTrue(store.save_market_snapshot(self.scan(False, "蜂蜜"), day))
        self.assertEqual(store.get_market_snapshot(day)["items"][0]["name"], "烤蜂蜜苹果")

    def test_market_keeps_coverage_separate_from_cartridge_quality(self):
        scan = self.scan()
        scan.update(cartridges_complete=False, unconfirmed_cartridges=["烤蜂蜜苹果"])
        scan["items"][0].update(target_cartridge="剧情游戏卡",
                               cartridge_evidence={"type_basis": "known_prefix", "number_basis": "number_conflict"})
        self.assertTrue(store.save_market_snapshot(scan, "today"))
        snapshot = store.get_market_snapshot("today")
        self.assertTrue(snapshot["complete"])
        self.assertFalse(snapshot["cartridges_complete"])
        self.assertEqual(snapshot["unconfirmed_cartridges"], ["烤蜂蜜苹果"])
        self.assertEqual(snapshot["items"][0]["cartridge_evidence"]["number_basis"], "number_conflict")

    def test_previous_cartridge_parser_cache_requires_new_scan(self):
        scan = self.scan()
        self.assertTrue(store.save_market_snapshot(scan, "today"))
        for version in (3, 4):
            MemorySharedStore.data["arbitrage"]["market"]["days"]["today"]["parser_version"] = version
            self.assertIsNone(store.get_market_snapshot("today"))

    def test_unconfirmed_names_or_prices_are_saved_but_not_reused_as_complete_market(self):
        for flag in ("names_complete", "prices_complete"):
            MemorySharedStore.data = {}
            scan = self.scan()
            scan[flag] = False
            self.assertTrue(store.save_market_snapshot(scan, "today"))
            self.assertTrue(MemorySharedStore.data["arbitrage"]["market"]["days"]["today"]["complete"])
            self.assertIsNone(store.get_market_snapshot("today"))

    def test_corrected_name_is_stored_canonically_with_original_evidence(self):
        scan = self.scan(name="萝卜缨")
        scan["items"][0].update(raw_name="蘿萄嬰", name_confirmed=True,
                                name_evidence={"basis": "one_character"})
        store.save_market_snapshot(scan, "today")
        row = store.get_market_snapshot("today")["items"][0]
        self.assertEqual(row["name"], "萝卜缨")
        self.assertEqual(row["raw_name"], "蘿萄嬰")

    def test_unknown_name_does_not_create_an_inventory_item(self):
        scan = self.scan(name="未确认错字")
        scan["items"][0]["name_confirmed"] = False
        scan.update(names_complete=False, unconfirmed_names=["未确认错字"])
        store.save_possession_snapshot(scan)
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        self.assertNotIn("未确认错字", inventory["items"])
        self.assertEqual(inventory["latest"]["possession"]["unconfirmed_names"], ["未确认错字"])

    def test_possession_observation_never_invents_or_erases_quantity(self):
        MemoryAccountStore.data = {
            "Pack_01@g_weekly": "2026-09-01 08:00:00",
            "arbitrage": {"inventory": {"items": {
                "烤蜂蜜苹果": {"quantity": 7, "quantity_status": "known"},
            }, "observations": [{"kind": "legacy"}]}},
        }
        scan = self.scan()
        scan.update({
            "sale_candidates_complete": True,
            "full_list_complete": False,
            "target_rate_floor": 120,
            "lowest_observed_rate": 118,
            "rate_order_safe": True,
        })
        self.assertTrue(store.save_possession_snapshot(scan))
        item = MemoryAccountStore.data["arbitrage"]["inventory"]["items"]["烤蜂蜜苹果"]
        self.assertEqual(item["quantity"], 7)
        self.assertTrue(item["present"])
        self.assertEqual(MemoryAccountStore.data["Pack_01@g_weekly"], "2026-09-01 08:00:00")
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        latest = inventory["latest"]["possession"]
        self.assertEqual(latest["item_names"], ["烤蜂蜜苹果"])
        self.assertNotIn("items", latest)
        self.assertEqual(latest["target_rate_floor"], 120)
        self.assertEqual(inventory["observations"], [{"kind": "legacy"}])
        self.assertEqual(inventory["events"][-1]["kind"], "possession_scan")

    def test_partial_cooking_observation_updates_only_readable_slots(self):
        record = {
            "recipe": "酱炒牛排",
            "observed_at": "2026-09-08T02:00:00+00:00",
            "complete": False,
            "materials": [
                {"name": "兽肉", "stock": 12, "status": "ok"},
                {"name": "西蓝花", "stock": None, "status": "unreadable"},
                {"name": "盐", "stock": 0, "status": "quantity_unverified"},
            ],
            "errors": ["slot_2:unreadable"],
        }
        self.assertTrue(store.save_cooking_stock_observation(record))
        items = MemoryAccountStore.data["arbitrage"]["inventory"]["items"]
        self.assertEqual(items["兽肉"]["quantity"], 12)
        self.assertNotIn("西蓝花", items)
        self.assertEqual(items["盐"]["quantity"], 0)
        self.assertFalse(items["盐"]["present"])
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        self.assertEqual(items["兽肉"]["quantity_observed_at"], record["observed_at"])
        latest = inventory["latest"]["cooking"]["酱炒牛排"]
        self.assertEqual(latest["last_attempt_at"], record["observed_at"])
        self.assertFalse(latest["complete"])
        self.assertNotIn("last_complete_at", latest)
        self.assertEqual(inventory["observations"], [])
        self.assertEqual(inventory["events"][-1]["error_count"], 1)

    def test_failed_cooking_read_does_not_refresh_last_successful_quantity(self):
        first_at = "2026-09-08T02:00:00+00:00"
        failed_at = "2026-09-08T03:00:00+00:00"
        self.assertTrue(store.save_cooking_stock_observation({
            "recipe": "酱炒牛排",
            "observed_at": first_at,
            "selected_count": 1,
            "complete": True,
            "materials": [{"name": "兽肉", "stock": 12, "status": "ok"}],
            "errors": [],
        }))
        self.assertTrue(store.save_cooking_stock_observation({
            "recipe": "酱炒牛排",
            "observed_at": failed_at,
            "selected_count": 1,
            "complete": False,
            "materials": [{"name": "兽肉", "stock": None, "status": "unreadable"}],
            "errors": ["slot_1:unreadable"],
        }))
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        item = inventory["items"]["兽肉"]
        latest = inventory["latest"]["cooking"]["酱炒牛排"]
        self.assertEqual(item["quantity"], 12)
        self.assertEqual(item["quantity_observed_at"], first_at)
        self.assertEqual(latest["last_attempt_at"], failed_at)
        self.assertEqual(latest["last_complete_at"], first_at)
        self.assertFalse(latest["complete"])

    def test_delta_updates_known_quantity_and_leaves_unknown_unknown(self):
        MemoryAccountStore.data = {"arbitrage": {"inventory": {"items": {
            "盐": {"quantity": 5, "quantity_status": "known"},
        }}}}
        result = store.apply_inventory_delta({"盐": 2, "胡椒": 3}, "purchase")
        self.assertEqual(result["applied"], {"盐": 7})
        self.assertEqual(result["unknown"], ["胡椒"])
        items = MemoryAccountStore.data["arbitrage"]["inventory"]["items"]
        self.assertEqual(items["盐"]["quantity"], 7)
        self.assertNotIn("quantity", items["胡椒"])
        conflict = store.apply_inventory_delta({"盐": -9}, "craft")
        self.assertEqual(conflict["conflicts"], ["盐"])
        self.assertNotIn("quantity", MemoryAccountStore.data["arbitrage"]["inventory"]["items"]["盐"])

    def test_confirmed_absolute_quantity_replaces_unknown(self):
        MemoryAccountStore.data = {"arbitrage": {"inventory": {"items": {
            "烤蜂蜜苹果": {"present": True, "quantity_status": "unknown"},
        }}}}
        self.assertTrue(store.set_inventory_quantities(
            {"烤蜂蜜苹果": 0}, "sell_exhausted", {"gold_delta": 100}
        ))
        item = MemoryAccountStore.data["arbitrage"]["inventory"]["items"]["烤蜂蜜苹果"]
        self.assertEqual(item["quantity"], 0)
        self.assertEqual(item["quantity_status"], "known")
        self.assertFalse(item["present"])

    def test_unknown_sale_quantity_invalidates_stale_fact_without_guessing_zero(self):
        MemoryAccountStore.data = {"arbitrage": {"inventory": {"items": {
            "烤蜂蜜苹果": {
                "quantity": 7,
                "quantity_status": "known",
                "quantity_observed_at": "2026-09-08T02:00:00+00:00",
                "present": True,
            },
        }}}}
        self.assertTrue(store.invalidate_inventory_quantities(
            ["烤蜂蜜苹果"], "sale_quantity_unreadable", {"gold_delta": 100}
        ))
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        item = inventory["items"]["烤蜂蜜苹果"]
        self.assertNotIn("quantity", item)
        self.assertNotIn("present", item)
        self.assertEqual(item["quantity_status"], "unknown")
        self.assertEqual(inventory["events"][-1]["kind"], "inventory_invalidate")


class SharedFileTests(unittest.TestCase):
    def test_shared_file_follows_account_storage_directory(self):
        attrs = ("_initialized", "_directory_initialized", "_account_ready", "_mode", "CONFIG_DIR",
                 "FILE_PATH", "BACKUP_PATH", "_degraded_readonly")
        account_state = {name: getattr(RealAccountStore, name) for name in attrs}
        shared_state = {name: getattr(RealSharedStore, name) for name in attrs}
        try:
            with tempfile.TemporaryDirectory(prefix="mfabd2-shared-store-") as temp:
                directory = Path(temp)
                RealAccountStore._initialized = True
                RealAccountStore._directory_initialized = True
                RealAccountStore._account_ready = False  # shared data needs no account
                RealAccountStore._mode = "portable"
                RealAccountStore.CONFIG_DIR = directory
                RealAccountStore.FILE_PATH = directory / "agent_save_data.json"
                RealAccountStore.BACKUP_PATH = directory / "agent_save_data.json.bak"
                RealAccountStore._degraded_readonly = False
                RealSharedStore._initialized = False
                RealSharedStore.CONFIG_DIR = None
                RealSharedStore.FILE_PATH = None
                RealSharedStore.BACKUP_PATH = None
                RealSharedStore._degraded_readonly = False

                self.assertTrue(RealSharedStore.set("probe", {"ok": True}))
                self.assertEqual(RealSharedStore.get("probe"), {"ok": True})
                self.assertTrue((directory / "agent_shared_data.json").is_file())
                self.assertTrue((directory / "agent_shared_data.json.bak").is_file())
                self.assertFalse((directory / "agent_save_data.json").exists())
        finally:
            for name, value in account_state.items():
                setattr(RealAccountStore, name, value)
            for name, value in shared_state.items():
                setattr(RealSharedStore, name, value)


if __name__ == "__main__":
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ArbitrageStoreTests))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(SharedFileTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
