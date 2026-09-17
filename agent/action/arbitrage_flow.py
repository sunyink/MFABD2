"""五阶段显式入口：料理依赖、按需仓检和共享行情。"""

from collections import OrderedDict
from dataclasses import replace

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from .arbitrage_replenish_buy import ensure_shop
from .bag_stock import bag_catalog, pending_materials, request_bag_materials
from .cooking_stock import get_cooking_scan_start
from .cartridge_lib import MarkCompleteAction
from utils import arbitrage_store as store, mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_purchase_lists import node_enabled
from utils.arbitrage_recipe_catalog import discover_recipe_entries
from utils.arbitrage_replenish_data import load_replenish_data
from utils.arbitrage_replenish_profit import DEFAULT_PROFIT_SURRENDER_PERCENT, validate_profit_surrender_percent
from utils.persistent_store import PersistentStore


STAGES = ("Arbitrage_PreSell_Entry", "Arbitrage_BuyItem", "Arbitrage_Cooking",
          "Arbitrage_SellItem", "Arbitrage_SellMaterials")
_COOKING_RUNS = OrderedDict()
_NORMAL_COMPLETED = OrderedDict()


def cooking_stage_active(task_id):
    return task_id in _COOKING_RUNS and _COOKING_RUNS[task_id] == PersistentStore._current_account_id


def replenishment_entries(context, task_id):
    """只为本轮实际打开过配方的料理补查、补买，不推测尚未解锁的配方。"""
    completed = _NORMAL_COMPLETED.get(task_id, {})
    if completed.get("account_id") != PersistentStore._current_account_id:
        return []
    selected = completed.get("selected", set())
    return [replace(entry, enabled=entry.enabled and bool(selected.intersection(entry.selectors)))
            for entry in discover_recipe_entries(context)]


@AgentServer.custom_action("ArbitrageCookingFinish")
class ArbitrageCookingFinish(CustomAction):
    def run(self, context, argv):
        if not sync_from_context(context, where="ArbitrageCookingFinish"):
            return False
        task_id = argv.task_detail.task_id
        if not cooking_stage_active(task_id):
            return False
        if argv.node_name == "Arbitrage_Cooking_ERREND":
            selected = {node.name for node in argv.task_detail.nodes
                        if node.completed and node.action is not None and node.action.success}
            _NORMAL_COMPLETED[task_id] = {"account_id": PersistentStore._current_account_id,
                                        "selected": selected}
            while len(_NORMAL_COMPLETED) > 16:
                _NORMAL_COMPLETED.popitem(last=False)
            return True
        if (_NORMAL_COMPLETED.get(task_id, {}).get("account_id") != PersistentStore._current_account_id
                or task_id not in _NORMAL_COMPLETED):
            mfaalog.error("[③料理] 未到达常规制作出口，不写入本周完成记录")
            return False
        return MarkCompleteAction().run(context, argv)


def _patch(context, nodes):
    if not context.override_pipeline(nodes):
        raise RuntimeError("阶段路线设置失败")


def replenish_budget(context):
    """None使用钱包余额；0明确没有预算。关闭上限时忽略隐藏输入。"""
    flags = context.get_node_object("Arbitrage_Cooking_Replenish").attach
    if type(flags.get("limit_budget")) is not bool:
        raise ValueError("补买支出上限开关必须为布尔")
    if not flags["limit_budget"]:
        return None
    value = flags.get("budget")
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or value < 0:
        raise ValueError("补买支出上限必须为非负整数")
    return value


def replenish_profit_surrender(context):
    flags = context.get_node_object("Arbitrage_Cooking_Replenish").attach
    value = flags.get("profit_surrender_percent", DEFAULT_PROFIT_SURRENDER_PERCENT)
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return validate_profit_surrender_percent(value)


@AgentServer.custom_action("ArbitrageStagePrepare")
class ArbitrageStagePrepare(CustomAction):
    def run(self, context, argv):
        try:
            active = [index for index, name in enumerate(STAGES, 1) if node_enabled(context, name)]
            route = context.get_node_object("Agt_Arbitrage_StageConfig").attach["start_next"]
            _patch(context, {"Arbitrage_Start": {"next": route if active else []}})
            mfaalog.info(f"[Arbitrage] 本轮阶段：{active}" if active else "[Arbitrage] 五个阶段全部关闭，直接结束")
            return True
        except Exception as exc:
            mfaalog.error(f"[Arbitrage] 阶段配置不可读：{exc}")
            return False


@AgentServer.custom_action("ArbitrageCookingPrepare")
class ArbitrageCookingPrepare(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="ArbitrageCookingPrepare"):
                return False
            entries = [entry for entry in discover_recipe_entries(context) if entry.enabled]
            if not entries:
                _patch(context, {"Arbitrage_Cooking": {"next": []}})
                mfaalog.info("[③料理] 制作名单为空，跳过制作、仓检和补买补做")
                return True
            _COOKING_RUNS[argv.task_detail.task_id] = PersistentStore._current_account_id
            _COOKING_RUNS.move_to_end(argv.task_detail.task_id)
            while len(_COOKING_RUNS) > 16:
                _COOKING_RUNS.popitem(last=False)
            replenish = node_enabled(context, "Arbitrage_Cooking_Replenish")
            need_inventory = replenish and replenish_budget(context) != 0
            _patch(context, {"Arbitrage_BagStockScan": {"enabled": need_inventory}})
            mfaalog.info(f"[③料理] 已选{len(entries)}道；缺料补买补做={'开启' if replenish else '关闭'}")
            return True
        except Exception as exc:
            mfaalog.error(f"[③料理] 准备失败：{exc}")
            return False


@AgentServer.custom_action("ArbitrageBagPrepare")
class ArbitrageBagPrepare(CustomAction):
    def run(self, context, argv):
        try:
            task_id = argv.task_detail.task_id
            if not sync_from_context(context, where="ArbitrageBagPrepare"):
                return False
            if not cooking_stage_active(task_id) or not node_enabled(context, "Arbitrage_Cooking_Replenish"):
                _patch(context, {argv.node_name: {"next": []}})
                return True
            data = load_replenish_data()
            # 读取常规制作及材料保护之后的最终状态，不能重新开启被跳过的料理。
            entries = [entry for entry in replenishment_entries(context, task_id) if entry.enabled]
            required = {name for entry in entries if entry.name in data["recipes"]
                        for name in data["recipes"][entry.name]["ingredients"]}
            request_bag_materials(task_id, required)
            config = context.get_node_object("Agt_BagStock_Config").attach
            catalog = bag_catalog(context, config)
            pending = pending_materials(dict.fromkeys(required), store.get_inventory_items(),
                                        get_cooking_scan_start(task_id))
            scan = set(pending) & catalog.keys()
            route = "Arbitrage_BagStockScan_Open" if scan else "Arbitrage_BagStockScan_Record"
            _patch(context, {argv.node_name: {"next": [route]}})
            mfaalog.info(f"[③仓检] 配方需要{len(required)}项，需进背包核对{len(scan)}项")
            return True
        except Exception as exc:
            mfaalog.error(f"[③仓检] 准备失败：{exc}")
            return False


@AgentServer.custom_action("ArbitrageMarketEnsure")
class ArbitrageMarketEnsure(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="ArbitrageMarketEnsure"):
                return False
            if store.get_market_snapshot() is not None:
                mfaalog.info("[行情] 复用今日完整行情")
                return True
            # Pipeline负责全盘页面准备和计数清理；扫描退出后下一阶段自行恢复商店。
            local = context.clone()
            if not local.set_anchor("Replenish_ShopEntry", "Arbitrage_Merchant_NoDiscount_Entry"):
                raise RuntimeError("行情进店入口设置失败")
            ensure_shop(local)
            result = local.run_task("Arbitrage_GlobalMarket_Entry")
            if (result is None or not result.status.succeeded
                    or not any(node.name == "Arbitrage_PriceList_Egress" for node in result.nodes)
                    or store.get_market_snapshot() is None):
                raise RuntimeError("今日完整行情未取得")
            return True
        except Exception as exc:
            mfaalog.error(f"[行情] 准备失败：{exc}")
            return False
