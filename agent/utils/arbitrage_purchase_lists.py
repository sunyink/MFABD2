"""采购默认表与每卡带自选表的合成，以及本轮已核实收藏状态。"""

from collections import OrderedDict
from datetime import datetime
from pathlib import Path
import json
import re

from .name_i18n import canon
from .persistent_store import PersistentStore


CATALOG_FILE = Path(__file__).resolve().parents[1] / "data" / "arbitrage_shop_catalog.json"
DATA_NODE = "Arbitrage_ShopBuy_Data_Csm"
PREPARE_NODE = "Arbitrage_Buy_ListPrepare"
# 固定规则，不接受界面或节点参数覆盖；出现时只允许取消收藏。
FORCED_UNFAVORITES = frozenset({"天赋神药"})
_RUNS = OrderedDict()


def load_purchase_catalog():
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))["cartridges"]


def parse_items(raw):
    if not isinstance(raw, str):
        raise ValueError("采购默认名单必须为字符串")
    return {canon(item.strip()) for item in re.split(r"[，,;|]+", raw) if item.strip()}


def resolve_purchase_table(defaults, catalog, overrides):
    """开启自选时整行替换；False条目不追加，空自选不回退默认。"""
    result = {}
    for cartridge, definition in catalog.items():
        if cartridge not in defaults:
            raise ValueError(f"采购默认表缺少卡带: {cartridge}")
        available = {canon(name) for name in definition["items"]} - FORCED_UNFAVORITES
        selected = parse_items(defaults[cartridge]) - FORCED_UNFAVORITES
        override = overrides.get(cartridge)
        if override is not None:
            if not isinstance(override, dict):
                raise ValueError(f"{cartridge}自选表与货架目录不一致")
            # 旧配置可能仍带已移除的天赋神药选项，忽略它而不是重新开放购买。
            override = {name: value for name, value in override.items()
                        if canon(name) not in FORCED_UNFAVORITES}
            if set(override) != set(definition["items"]) - FORCED_UNFAVORITES:
                raise ValueError(f"{cartridge}自选表与货架目录不一致")
            if any(type(value) is not bool for value in override.values()):
                raise ValueError(f"{cartridge}自选值必须为布尔")
            selected = {canon(name) for name, value in override.items() if value}
        if not selected <= available:
            raise ValueError(f"{cartridge}采购名单包含目录外商品: {selected - available}")
        result[cartridge] = selected
    return result


def changed_cartridges(table, applied, reset_timestamp, every_run=False):
    pending = []
    for cartridge, selected in table.items():
        previous = applied.get(cartridge, {})
        try:
            timestamp = datetime.fromisoformat(previous["applied_at"])
            fresh = timestamp.tzinfo is not None and timestamp.timestamp() >= reset_timestamp
            items = previous["items"]
            same = (isinstance(items, list) and all(isinstance(item, str) for item in items)
                    and {canon(item) for item in items} == selected)
        except (KeyError, TypeError, ValueError):
            fresh = same = False
        if every_run or not fresh or not same:
            pending.append(cartridge)
    return pending


def put_purchase_run(task_id, table, pending):
    _RUNS[task_id] = {"account_id": PersistentStore._current_account_id,
                      "table": table, "pending": set(pending), "failed_cards": {}}
    _RUNS.move_to_end(task_id)
    while len(_RUNS) > 16:
        _RUNS.popitem(last=False)
    return _RUNS[task_id]


def clear_purchase_run(task_id):
    _RUNS.pop(task_id, None)


def get_purchase_run(task_id):
    value = _RUNS.get(task_id)
    if value is not None and value["account_id"] != PersistentStore._current_account_id:
        raise ValueError("采购过程中账号改变，旧名单不可继续使用")
    return value


def completed_purchase_items(task_id, day):
    """仅排除本轮已确认常规采购、且收藏核实过的最终名单，不把默认表当成交。"""
    run = get_purchase_run(task_id)
    if run is None or run.get("completed_day") != day:
        return set()
    unverified = run["pending"] | run["failed_cards"].keys()
    return {(canon(cartridge.split(":", 1)[1]), item)
            for cartridge, items in run["table"].items() if cartridge not in unverified
            for item in items}


def node_enabled(context, name):
    node = context.get_node_data(name)
    if not isinstance(node, dict):
        raise ValueError(f"配置节点不可读: {name}")
    return node.get("enabled", True) and node.get("max_hit", 2 ** 32 - 1) != 0
