"""管路显式采购准备：实际名单、周刷新、变化检测与批量购买放行。"""

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from .cartridge_lib import CooldownManager
from utils import arbitrage_store as store, mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_purchase_lists import (
    DATA_NODE, PREPARE_NODE, load_purchase_catalog, resolve_purchase_table,
    changed_cartridges, put_purchase_run, get_purchase_run, node_enabled,
)


@AgentServer.custom_action("ArbitrageBuyListPrepare")
class ArbitrageBuyListPrepare(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="ArbitrageBuyListPrepare"):
                return False
            config = context.get_node_object(PREPARE_NODE).attach
            for key in ("custom_enabled", "scan_every_run"):
                if type(config.get(key)) is not bool:
                    raise ValueError(f"采购参数{key}必须为布尔")
            catalog = load_purchase_catalog()
            defaults = context.get_node_object(DATA_NODE).attach
            overrides = {}
            if config["custom_enabled"]:
                for cartridge, definition in catalog.items():
                    node = definition["custom_node"]
                    if node_enabled(context, node):
                        overrides[cartridge] = context.get_node_object(node).attach
            table = resolve_purchase_table(defaults, catalog, overrides)
            reset, _ = CooldownManager()._calculate_server_reset_timestamp("g_weekly")
            pending = changed_cartridges(table, store.get_purchase_alignments(), reset, config["scan_every_run"])
            put_purchase_run(argv.task_detail.task_id, table, pending)
            # 仅改变卡带派发开关，完整保留原卡带查找、滑动和成功后的回跳链。
            patch = {definition["selector"]: {"enabled": cartridge in pending}
                     for cartridge, definition in catalog.items()}
            patch["Arbitrage_Favorit_Buy"] = {"enabled": True}
            if not context.override_pipeline(patch):
                raise RuntimeError("采购卡带派发设置失败")
            mfaalog.info(f"[②采购] 实际清单已生成：{len(table)}张卡带，需更新收藏{len(pending)}张")
            for cartridge in pending:
                mfaalog.info(f"[②采购] {cartridge}：更新收藏 → {', '.join(sorted(table[cartridge])) or '清空收藏'}")
            return True
        except Exception as exc:
            mfaalog.error(f"[②采购] 清单准备失败：{exc}")
            return False


@AgentServer.custom_recognition("ArbitrageBuyNeedsScan")
class ArbitrageBuyNeedsScan(CustomRecognition):
    def analyze(self, context, argv):
        try:
            run = get_purchase_run(argv.task_detail.task_id)
            if run and run["pending"] and not run["failed"]:
                return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"pending": sorted(run["pending"])})
        except Exception as exc:
            mfaalog.error(f"[②采购] 收藏更新判定失败：{exc}")
        return None


@AgentServer.custom_action("ArbitrageBuyReady")
class ArbitrageBuyReady(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="ArbitrageBuyReady"):
                return False
            run = get_purchase_run(argv.task_detail.task_id)
            if run is None or run["failed"] or run["pending"]:
                pending = sorted(run["pending"]) if run else []
                mfaalog.warning(f"[②采购] 收藏未全部核实，不进行一键购买：{pending}")
                return False
            return True
        except Exception as exc:
            mfaalog.error(f"[②采购] 购买放行检查失败：{exc}")
            return False


@AgentServer.custom_action("ArbitrageBuyAbort")
class ArbitrageBuyAbort(CustomAction):
    def run(self, context, argv):
        try:
            if not context.override_pipeline({"Arbitrage_Favorit_Buy": {"enabled": False},
                                              "Arbitrage_Buy_Select_Str": {"next": []}}):
                return False
            run = get_purchase_run(argv.task_detail.task_id)
            if run is not None:
                run["failed"] = True
            mfaalog.warning("[②采购] 本轮常规购买已取消，未确认的收藏记录保留待下次核对")
            return True
        except Exception as exc:
            mfaalog.error(f"[②采购] 取消采购失败：{exc}")
            return False
