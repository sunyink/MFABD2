"""Bounded hot inventory events and append-only, account-local cold history.

Only committed hot records may reach the archive. Position payloads stay in the
hot retry window until acknowledged; the journal contains marker metadata, not
old copies of the daily snapshots. This module never restores runtime inventory.
"""

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from . import mfaalog


RETENTION_DAYS = 62
BATCH_SIZE = 16
EVENT_LIMIT = 200
_WRITERS = OrderedDict()
_LOCK = threading.RLock()
_DAY_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})\.(events\.jsonl|positions\.json)$")
_FACT_FIELDS = ("quantity", "quantity_status", "quantity_observed_at", "quantity_source",
                "quantity_invalidated_at", "present", "presence_observed_at", "presence_source")


def now():
    return datetime.now(timezone.utc).isoformat()


def day_of(timestamp):
    value = datetime.fromisoformat(timestamp)
    if value.tzinfo is None:
        raise ValueError("归档时间缺少时区")
    return value.astimezone(timezone.utc).date().isoformat()


def inventory_facts(items, names=None):
    result = {}
    for name in items if names is None else names:
        row = items.get(name, {})
        fact = {key: deepcopy(row[key]) for key in _FACT_FIELDS if key in row}
        if (fact.get("quantity_status") != "known" or type(fact.get("quantity")) is not int
                or fact["quantity"] < 0):
            fact.pop("quantity", None)
            fact["quantity_status"] = "unknown"
        result[name] = fact
    return result


def identity(store):
    store._init_paths()
    if store.CONFIG_DIR is None:
        raise RuntimeError("账号存档目录不可用")
    return Path(store.CONFIG_DIR).resolve(), str(store._current_account_id)


@contextmanager
def account_lock(directory):
    """OS releases the lock on process exit; contention never waits for a game task."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path, data):
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ColdArchive:
    def __init__(self, directory, account):
        self.account = account
        self.directory = Path(directory) / "inventory_history" / sha256(account.encode("utf-8")).hexdigest()
        self.acked = set()
        self.recovered = False

    def _load_day(self, day):
        path = self.directory / f"{day}.positions.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("account_id") != self.account
                    or data.get("archive_day") != day or data.get("schema_version") != 1):
                raise ValueError("冷档位置表格式或账号不符，保留原文件")
            return data
        return {"schema_version": 1, "account_id": self.account, "archive_day": day,
                "positions": {}, "runs": {}, "gaps": {}, "journal_damage": []}

    @staticmethod
    def _position(data, event, missing=False):
        slots = data["positions"].setdefault(event["series"], {})
        old = slots.get(event["position"])
        order = (event["recorded_at"], event["event_id"])
        if old:
            old_order = (old["recorded_at"], old["event_id"])
            if event["keep"] == "first" and old_order <= order:
                return
            if event["keep"] == "last" and old_order >= order:
                return
        snapshot = deepcopy(event.get("_snapshot"))
        slots[event["position"]] = {
            key: deepcopy(event[key]) for key in
            ("event_id", "recorded_at", "run_id", "phase", "keep", "cross_day")
        }
        slot = slots[event["position"]]
        slot["snapshot_missing"] = missing or snapshot is None
        if not slot["snapshot_missing"]:
            slot["items"] = snapshot
            slot["known_item_count"] = sum(fact.get("quantity_status") == "known" for fact in snapshot.values())
            slot["unknown_item_names"] = [name for name, fact in snapshot.items()
                                          if fact.get("quantity_status") != "known"]
        # A snapshot is only an inventory view, never a claim of full observation.

    def _project_marker(self, data, event):
        run = data["runs"].setdefault(event["run_id"], {
            "series": event["series"], "started_at": event["started_at"],
            "status": "no_end_evidence", "begin_missing": event["phase"] != "begin",
        })
        if event["phase"] == "begin":
            run["begin_missing"] = False
        elif event["phase"] == "end":
            run.update(status="ended", ended_at=event["recorded_at"], cross_day=event["cross_day"])
        self._position(data, event)

    def flush(self, events, gaps=()):
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()
        groups = {}
        expired = set()
        for event in events:
            day = event.get("archive_day") or day_of(event["recorded_at"])
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("无效归档日期")
            if day < cutoff:
                expired.add(event["event_id"])
            else:
                groups.setdefault(day, []).append(event)
        gap_days = {}
        for row in gaps:
            day = row["archive_day"]
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("无效缺口日期")
            if day >= cutoff:
                gap_days[day] = row
        acknowledged = set(expired)
        with account_lock(self.directory):
            for day in sorted(groups.keys() | gap_days.keys()):
                data = self._load_day(day)
                original = deepcopy(data)
                path = self.directory / f"{day}.events.jsonl"
                existing = path.read_bytes() if path.exists() else b""
                ids = set()
                damage = set(data["journal_damage"])
                for line in existing.splitlines():
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or not isinstance(row.get("event_id"), str):
                            raise ValueError("无事件编号")
                        ids.add(row["event_id"])
                    except (ValueError, UnicodeError):
                        damage.add(sha256(line).hexdigest())
                data["journal_damage"] = sorted(damage)
                if day in gap_days:
                    gap = gap_days[day]
                    previous = data["gaps"].get(gap["gap_id"], {})
                    if gap["count"] >= previous.get("count", 0):
                        data["gaps"][gap["gap_id"]] = deepcopy(gap)
                    for marker in gap.get("missing_first", {}).values():
                        self._position(data, marker, missing=True)
                fresh = []
                for event in sorted(groups.get(day, []), key=lambda row: (row["recorded_at"], row["event_id"])):
                    if event["event_id"] in ids:
                        acknowledged.add(event["event_id"])
                        continue
                    if event["kind"] == "inventory_position":
                        self._project_marker(data, event)
                    fresh.append({key: value for key, value in event.items()
                                  if key not in ("_snapshot", "archived")})
                    ids.add(event["event_id"])
                # Projection first: replay after a crash is safe; the hot record still
                # holds the captured snapshot until both files have been committed.
                if data != original or not (self.directory / f"{day}.positions.json").exists():
                    _atomic_json(self.directory / f"{day}.positions.json", data)
                if fresh or existing and not existing.endswith(b"\n"):
                    with path.open("ab") as handle:
                        if existing and not existing.endswith(b"\n"):
                            handle.write(b"\n")  # preserve damaged bytes; never truncate history
                        for row in fresh:
                            handle.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                        handle.flush()
                        os.fsync(handle.fileno())
                acknowledged.update(row["event_id"] for row in fresh)
            # Only direct, dated files owned by this archive are eligible for cleanup.
            root = self.directory.resolve()
            for path in self.directory.iterdir():
                match = _DAY_FILE.fullmatch(path.name)
                if match and match[1] < cutoff and path.is_file() and path.resolve().parent == root:
                    path.unlink()
        self.acked.update(acknowledged)
        return acknowledged


def _writer(store):
    key = identity(store)
    if key not in _WRITERS:
        _WRITERS[key] = ColdArchive(*key)
    _WRITERS.move_to_end(key)
    while len(_WRITERS) > 16:
        _WRITERS.popitem(last=False)
    return _WRITERS[key]


def _pending(events):
    return [event for event in events if event.get("event_id") and not event.get("archived")]


def _acknowledge(events, writer):
    for event in events:
        if event.get("event_id") in writer.acked:
            event["archived"] = True
            event.pop("_snapshot", None)
    # The bounded hot window is enough for in-process acknowledgements too.
    writer.acked.intersection_update(event.get("event_id") for event in events)


def _try_flush(writer, events, gaps):
    try:
        writer.flush(_pending(events), gaps.values())
        _acknowledge(events, writer)
        writer.recovered = True
        return True
    except Exception as exc:
        mfaalog.warning(f"[InventoryArchive] 冷档暂未完成，保留热档内事件供重试：{exc}")
        return False


def _record_gap(gaps, event):
    day = event.get("archive_day") or day_of(event["recorded_at"])
    gap = gaps.setdefault(day, {"gap_id": uuid4().hex, "archive_day": day, "count": 0,
                                "first_at": event["recorded_at"], "missing_first": {}})
    gap["count"] += 1
    gap["last_at"] = event["recorded_at"]
    if event.get("kind") == "inventory_position" and event["keep"] == "first":
        key = json.dumps([event["series"], event["position"]], ensure_ascii=False)
        marker = {key: value for key, value in event.items() if key not in ("_snapshot", "archived")}
        previous = gap["missing_first"].get(key)
        if previous is None or marker["recorded_at"] < previous["recorded_at"]:
            gap["missing_first"][key] = marker


def commit_inventory(data, inventory, store, *, force=False):
    """The caller has appended one new event; archive errors never change save's result."""
    with _LOCK:
        events = inventory["events"]
        event = events[-1]
        event.setdefault("event_id", uuid4().hex)
        event.setdefault("recorded_at", now())
        event.setdefault("archive_day", day_of(event["recorded_at"]))
        event.setdefault("account_id", str(getattr(store, "_current_account_id", "0")))
        gaps = inventory.setdefault("archive_gaps", {})
        writer = None
        try:
            writer = _writer(store)
            _acknowledge(events, writer)
            if _pending(events[:-EVENT_LIMIT]):
                # New event isn't committed yet. Only export the existing hot window.
                _try_flush(writer, events[:-1], gaps)
                _acknowledge(events, writer)
        except Exception as exc:
            mfaalog.warning(f"[InventoryArchive] 无法准备冷档：{exc}")
        evicted = events[:-EVENT_LIMIT]
        for old in evicted:
            if old.get("event_id") and not old.get("archived"):
                _record_gap(gaps, old)
        if evicted:
            del events[:-EVENT_LIMIT]
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()
        for day in list(gaps):
            if day < cutoff:
                del gaps[day]
        if not store.save(data):
            return False
        if writer and (force or not writer.recovered or len(_pending(events)) >= BATCH_SIZE):
            _try_flush(writer, events, gaps)
        return True
