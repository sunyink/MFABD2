"""Inventory history regression tests. Temporary archives only; no user save access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from utils import inventory_archive as archive, arbitrage_store as store
from utils.persistent_store import PersistentStore


class MemoryStore:
    CONFIG_DIR = None
    _current_account_id = "test/账号"
    _degraded_readonly = False
    data = {}
    fail_save = False

    @classmethod
    def _init_paths(cls): pass

    @classmethod
    def load(cls): return deepcopy(cls.data)

    @classmethod
    def save(cls, data):
        if cls.fail_save:
            return False
        cls.data = deepcopy(data)
        return True


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mfabd2-inventory-history-")
        self.addCleanup(self.temp.cleanup)
        MemoryStore.CONFIG_DIR = Path(self.temp.name)
        MemoryStore.data = {}
        MemoryStore.fail_save = False
        MemoryStore._current_account_id = "test/账号"
        MemoryStore._degraded_readonly = False
        for mock in (patch.object(store, "PersistentStore", MemoryStore),
                     patch.dict(archive._WRITERS, clear=True),
                     patch.object(archive.mfaalog, "warning"), patch.object(archive.mfaalog, "info")):
            mock.start()
            self.addCleanup(mock.stop)
        self.today = datetime.now(timezone.utc).date().isoformat()

    def writer(self): return archive._writer(MemoryStore)

    def timestamp(self, seconds=0, day=None):
        return f"{day or self.today}T{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}+00:00"

    def marker(self, position="A", keep="first", phase="begin", run="one", seconds=1, day=None,
               capture_day=None, series="arbitrage"):
        data = MemoryStore.load()
        inventory = store._inventory(data)
        stamp = self.timestamp(seconds, capture_day or day)
        inventory["events"].append({
            "kind": "inventory_position", "recorded_at": stamp, "observed_at": stamp,
            "series": series, "position": position, "phase": phase, "keep": keep,
            "run_id": run, "started_at": self.timestamp(0, day), "archive_day": day or self.today,
            "cross_day": archive.day_of(stamp) != (day or self.today),
            "_snapshot": archive.inventory_facts(inventory["items"]),
        })
        self.assertTrue(store._save_inventory(data, force=True))

    def state(self, day=None):
        return self.writer()._load_day(day or self.today)

    def rows(self, day=None):
        path = self.writer().directory / f"{day or self.today}.events.jsonl"
        if not path.exists(): return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class InventoryArchiveTests(ArchiveFixture):
    def test_first_a_last_b_and_no_old_b_snapshots_in_journal(self):
        store.set_inventory_quantities({"蜂蜜": 100}, "test")
        self.marker()
        store.set_inventory_quantities({"蜂蜜": 80}, "test")
        self.marker("B", "last", "end", seconds=2)
        self.marker(run="two", seconds=3)
        store.set_inventory_quantities({"蜂蜜": 60}, "test")
        self.marker("B", "last", "end", run="two", seconds=4)
        slots = self.state()["positions"]["arbitrage"]
        self.assertEqual(slots["A"]["items"]["蜂蜜"]["quantity"], 100)
        self.assertEqual(slots["B"]["items"]["蜂蜜"]["quantity"], 60)
        self.assertEqual(slots["B"]["run_id"], "two")
        self.assertTrue(all("_snapshot" not in row for row in self.rows()))

    def test_later_unfinished_run_preserves_completed_b(self):
        self.marker()
        self.marker("B", "last", "end", seconds=2)
        self.marker(run="unfinished", seconds=3)
        data = self.state()
        self.assertEqual(data["positions"]["arbitrage"]["B"]["run_id"], "one")
        self.assertEqual(data["runs"]["one"]["status"], "ended")
        self.assertEqual(data["runs"]["unfinished"]["status"], "no_end_evidence")

    def test_cross_day_end_stays_with_begin_day(self):
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        self.marker(day=yesterday)
        self.marker("B", "last", "end", day=yesterday, capture_day=self.today)
        self.assertTrue(self.state(yesterday)["positions"]["arbitrage"]["B"]["cross_day"])
        self.assertFalse((self.writer().directory / f"{self.today}.positions.json").exists())

    def test_other_series_and_position_are_reusable(self):
        self.marker("before", series="gather")
        self.marker("middle", "last", "snapshot", seconds=2, series="gather")
        self.assertEqual(set(self.state()["positions"]["gather"]), {"before", "middle"})

    def test_confirmed_delta_unknown_and_stale_observation_stay_distinct(self):
        old_time = self.timestamp(1, (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
        with patch.object(store, "utc_now", return_value=old_time):
            store.set_inventory_quantities({"蜂蜜": 10}, "bag_detail")
        self.marker()
        fact = self.state()["positions"]["arbitrage"]["A"]["items"]["蜂蜜"]
        self.assertEqual(fact["quantity_observed_at"], old_time)
        store.apply_inventory_delta({"蜂蜜": 2, "未知材料": 3}, "confirmed_purchase")
        store.invalidate_inventory_quantities(["蜂蜜"], "cooking_attempt")
        self.marker("B", "last", "end", seconds=2)
        fact = self.state()["positions"]["arbitrage"]["B"]["items"]["蜂蜜"]
        self.assertNotIn("quantity", fact)
        self.assertEqual(fact["quantity_status"], "unknown")
        delta = next(row for row in self.rows() if row["kind"] == "inventory_delta")
        self.assertEqual(delta["changes"]["蜂蜜"], 2)
        self.assertEqual(delta["item_facts"]["蜂蜜"]["quantity"], 12)
        self.assertNotIn("quantity", delta["item_facts"]["未知材料"])

    def test_cooking_records_actual_quantities_and_unreadable_names(self):
        store.save_cooking_stock_observation({"recipe": "菜", "complete": False,
                                            "materials": [{"name": "蜂蜜", "stock": 7},
                                                          {"name": "肉桂", "stock": None}]})
        self.marker()
        row = next(row for row in self.rows() if row["kind"] == "cooking_stock")
        self.assertEqual(row["quantities"], {"蜂蜜": 7})
        self.assertEqual(row["unreadable_item_names"], ["肉桂"])
        self.assertEqual(row["item_facts"]["蜂蜜"]["quantity_source"], "cooking_menu")

    def test_old_events_are_not_reconstructed(self):
        MemoryStore.data = {"arbitrage": {"inventory": {"events": [{"kind": "old", "items": ["蜂蜜"]}]}}}
        self.marker()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(MemoryStore.data["arbitrage"]["inventory"]["events"][0]["kind"], "old")

    def test_batch_size_and_hot_window_do_not_force_a_write_per_event(self):
        store.set_inventory_quantities({"蜂蜜": 1}, "test")  # startup recovery
        for number in range(2, 17):
            store.set_inventory_quantities({"蜂蜜": number}, "test")
        self.assertEqual(len(self.rows()), 1)
        store.set_inventory_quantities({"蜂蜜": 17}, "test")
        self.assertEqual(len(self.rows()), 17)
        for number in range(18, 218):
            store.set_inventory_quantities({"蜂蜜": number}, "test")
        self.assertEqual(len(MemoryStore.data["arbitrage"]["inventory"]["events"]), 200)
        self.assertEqual(len(self.rows()), 209)
        self.marker()
        self.assertEqual(len(self.rows()), 218)

    def test_restart_replays_recent_unacknowledged_events_once(self):
        store.set_inventory_quantities({"蜂蜜": 1}, "test")
        store.set_inventory_quantities({"蜂蜜": 2}, "test")
        original_id = MemoryStore.data["arbitrage"]["inventory"]["events"][-1]["event_id"]
        archive._WRITERS.clear()
        self.marker()
        self.assertEqual(sum(row["event_id"] == original_id for row in self.rows()), 1)
        self.assertEqual(len({row["event_id"] for row in self.rows()}), len(self.rows()))

    def test_failed_primary_save_never_exports_uncommitted_event(self):
        MemoryStore.fail_save = True
        self.assertFalse(store.set_inventory_quantities({"蜂蜜": 12}, "test"))
        self.assertEqual(self.rows(), [])
        self.assertEqual(MemoryStore.data, {})

    def test_cold_failure_retries_captured_a_not_later_inventory(self):
        store.set_inventory_quantities({"蜂蜜": 12}, "test")
        with patch.object(archive.ColdArchive, "flush", side_effect=OSError("disk unavailable")):
            self.marker()
            self.assertTrue(store.set_inventory_quantities({"蜂蜜": 34}, "test"))
        self.marker(run="second", seconds=2)
        self.assertEqual(self.state()["positions"]["arbitrage"]["A"]["items"]["蜂蜜"]["quantity"], 12)

    def test_eviction_failure_records_gap_and_missing_first_a(self):
        with patch.object(archive.ColdArchive, "flush", side_effect=OSError("disk unavailable")):
            self.marker()
            for number in range(205):
                self.assertTrue(store.set_inventory_quantities({"蜂蜜": number}, "test"))
        self.marker(run="second", seconds=2)
        day = self.state()
        self.assertEqual(sum(gap["count"] for gap in day["gaps"].values()), 6)
        self.assertTrue(day["positions"]["arbitrage"]["A"]["snapshot_missing"])
        self.assertEqual(day["positions"]["arbitrage"]["A"]["run_id"], "one")

    def test_snapshot_payloads_leave_hot_events_after_acknowledgement(self):
        store.set_inventory_quantities({"蜂蜜": 12}, "test")
        self.marker()
        store.set_inventory_quantities({"蜂蜜": 13}, "test")
        marker = next(row for row in MemoryStore.data["arbitrage"]["inventory"]["events"]
                      if row["kind"] == "inventory_position")
        self.assertNotIn("_snapshot", marker)
        self.assertTrue(marker["archived"])

    def test_incomplete_tail_is_preserved_and_marked(self):
        self.marker()
        path = self.writer().directory / f"{self.today}.events.jsonl"
        before = path.read_bytes() + b'{"event_id":"broken'
        path.write_bytes(before)
        self.marker("B", "last", "end", seconds=2)
        self.assertTrue(path.read_bytes().startswith(before + b"\n"))
        self.assertEqual(len(self.state()["journal_damage"]), 1)
        self.assertEqual(json.loads(path.read_bytes().splitlines()[-1])["phase"], "end")

    def test_crash_after_projection_before_journal_is_replayable(self):
        self.marker()
        path = self.writer().directory / f"{self.today}.events.jsonl"
        path.unlink()  # simulates a crash before the first journal append
        archive._WRITERS.clear()
        self.marker(run="second", seconds=2)
        self.assertEqual(self.state()["positions"]["arbitrage"]["A"]["run_id"], "one")
        self.assertEqual(len(self.rows()), 2)

    def test_invalid_position_file_is_not_overwritten(self):
        self.marker()
        path = self.writer().directory / f"{self.today}.positions.json"
        path.write_text("broken", encoding="utf-8")
        self.marker("B", "last", "end", seconds=2)
        self.assertEqual(path.read_text(), "broken")

    def test_failed_position_replace_keeps_last_b_and_replays_original_capture(self):
        self.marker()
        store.set_inventory_quantities({"蜂蜜": 10}, "test")
        self.marker("B", "last", "end", seconds=2)
        store.set_inventory_quantities({"蜂蜜": 20}, "test")
        with patch.object(archive.os, "replace", side_effect=OSError("replace unavailable")):
            self.marker("B", "last", "end", run="two", seconds=3)
        self.assertEqual(self.state()["positions"]["arbitrage"]["B"]["items"]["蜂蜜"]["quantity"], 10)
        store.set_inventory_quantities({"蜂蜜": 30}, "test")
        self.marker("C", "last", "snapshot", run="two", seconds=4)
        self.assertEqual(self.state()["positions"]["arbitrage"]["B"]["items"]["蜂蜜"]["quantity"], 20)

    def test_real_save_and_backup_commit_before_cold_archive(self):
        directory = Path(self.temp.name)
        attributes = {"_initialized": True, "_directory_initialized": True, "_account_ready": True,
                      "_mode": "portable", "CONFIG_DIR": directory,
                      "FILE_PATH": directory / "agent_save_data.json",
                      "BACKUP_PATH": directory / "agent_save_data.json.bak",
                      "_current_account_id": "0", "_sanitized_account_id": "0", "_degraded_readonly": False}
        from contextlib import ExitStack
        with ExitStack() as mocks:
            for name, value in attributes.items():
                mocks.enter_context(patch.object(PersistentStore, name, value))
            mocks.enter_context(patch.object(store, "PersistentStore", PersistentStore))
            self.assertTrue(store.set_inventory_quantities({"蜂蜜": 12}, "bag_detail"))
            saved = json.loads(PersistentStore.FILE_PATH.read_text(encoding="utf-8"))
            backup = json.loads(PersistentStore.BACKUP_PATH.read_text(encoding="utf-8"))
            self.assertEqual(saved, backup)
            event = saved["arbitrage"]["inventory"]["events"][-1]
            path = archive._writer(PersistentStore).directory / f"{self.today}.events.jsonl"
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["event_id"], event["event_id"])

    def test_retention_deletes_only_owned_old_files(self):
        self.marker()
        directory = self.writer().directory
        old = (datetime.now(timezone.utc).date() - timedelta(days=62)).isoformat()
        retained = (datetime.now(timezone.utc).date() - timedelta(days=61)).isoformat()
        for day in (old, retained):
            (directory / f"{day}.events.jsonl").write_text("", encoding="utf-8")
        (directory / "notes.txt").write_text("keep", encoding="utf-8")
        self.marker("B", "last", "end", seconds=2)
        self.assertFalse((directory / f"{old}.events.jsonl").exists())
        self.assertTrue((directory / f"{retained}.events.jsonl").exists())
        self.assertTrue((directory / "notes.txt").exists())

    def test_account_names_cannot_escape_or_collide_after_sanitizing(self):
        self.marker()
        first = self.writer().directory
        MemoryStore._current_account_id = "test_账号"
        MemoryStore.data = {}
        self.marker()
        self.assertNotEqual(first, self.writer().directory)
        self.assertEqual(self.writer().directory.parent, Path(self.temp.name).resolve() / "inventory_history")

    def test_file_lock_rejects_another_process_then_releases(self):
        code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                "from utils.inventory_archive import account_lock; "
                "\nwith account_lock(Path(sys.argv[2])): print('locked')")
        command = [sys.executable, "-B", "-c", code, str(ROOT / "agent"), str(self.writer().directory)]
        with archive.account_lock(self.writer().directory):
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
