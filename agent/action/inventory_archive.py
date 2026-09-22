"""Reusable inventory position action; no game input and no inventory refresh."""

from collections import OrderedDict
import json
from uuid import uuid4

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from utils import arbitrage_store as store, inventory_archive as history, mfaalog
from utils.account_sync import sync_from_context
from utils.persistent_store import PersistentStore


_RUNS = OrderedDict()


@AgentServer.custom_action("InventoryArchive")
class InventoryArchive(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="InventoryArchive"):
                raise RuntimeError("当前账号未确认")
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            series, position = params["series"], params["position"]
            phase, keep = params["phase"], params["keep"]
            if any(not isinstance(value, str) or not value.strip() or len(value) > 80
                   for value in (series, position)):
                raise ValueError("series/position 必须为 1～80 字符的非空代号")
            if phase not in ("begin", "snapshot", "end") or keep not in ("first", "last"):
                raise ValueError("未知归档阶段或位置保留规则")
            identity = history.identity(PersistentStore)
            key = (argv.task_detail.task_id, series)
            captured = history.now()
            with store._LOCK:
                run = _RUNS.get(key)
                if run is not None and run["identity"] != identity:
                    raise RuntimeError("运行中账号变化，本轮位置不写入新账号")
                if phase == "begin":
                    if run is not None:
                        return True
                    run = {"identity": identity, "run_id": uuid4().hex, "started_at": captured,
                           "archive_day": history.day_of(captured), "saved": False, "ended": False}
                    _RUNS[key] = run
                    while len(_RUNS) > 64:
                        _RUNS.popitem(last=False)
                elif run is None or not run["saved"] or run["ended"]:
                    raise RuntimeError("缺少本任务已保存的开始记录，或本轮已结束")
                data = PersistentStore.load()
                if PersistentStore._degraded_readonly:
                    raise RuntimeError("主存档不可读，不能用空视图归档")
                inventory = store._inventory(data)
                inventory["events"].append({
                    "kind": "inventory_position", "recorded_at": captured, "observed_at": captured,
                    "series": series, "position": position, "keep": keep, "phase": phase,
                    "run_id": run["run_id"], "started_at": run["started_at"],
                    "archive_day": run["archive_day"],
                    "cross_day": history.day_of(captured) != run["archive_day"],
                    "_snapshot": history.inventory_facts(inventory["items"]),
                })
                saved = store._save_inventory(data, force=True)
                if phase == "begin":
                    run["saved"] = saved
                if not saved:
                    raise RuntimeError("实时存档保存失败，该位置未归档")
                if phase == "end":
                    run["ended"] = True
                mfaalog.info(f"[InventoryArchive] {series}/{position} 已捕获，"
                             f"规则={keep}，阶段={phase}；库存沿用已有证据")
        except Exception as exc:
            mfaalog.warning(f"[InventoryArchive] 位置记录未完成，不阻断业务：{exc}")
        return True
