"""采购名单与扫描数据准备；扫描周期检查和打标由 Pipeline 负责。"""

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils import arbitrage_store as store, mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_purchase_lists import (
    DATA_NODE, PREPARE_NODE, load_purchase_catalog, resolve_purchase_table,
    changed_cartridges, put_purchase_run, get_purchase_run, clear_purchase_run, node_enabled,
)


@AgentServer.custom_action("ArbitrageBuyListPrepare")
class ArbitrageBuyListPrepare(CustomAction):
    def run(self, context, argv):
        clear_purchase_run(argv.task_detail.task_id)
        try:
            if not sync_from_context(context, where="ArbitrageBuyListPrepare"):
                return False
            config = context.get_node_object(PREPARE_NODE).attach
            if type(config.get("custom_enabled")) is not bool:
                raise ValueError("采购参数custom_enabled必须为布尔")
            catalog = load_purchase_catalog()
            defaults = context.get_node_object(DATA_NODE).attach
            overrides = {}
            if config["custom_enabled"]:
                for cartridge, definition in catalog.items():
                    node = definition["custom_node"]
                    if node_enabled(context, node):
                        overrides[cartridge] = context.get_node_object(node).attach
            table = resolve_purchase_table(defaults, catalog, overrides)
            pending = changed_cartridges(table, store.get_purchase_alignments())
            put_purchase_run(argv.task_detail.task_id, table, pending)
            # 仅改变卡带派发开关，完整保留原卡带查找、滑动和成功后的回跳链。
            patch = {definition["selector"]: {"enabled": cartridge in pending}
                     for cartridge, definition in catalog.items()}
            if not context.override_pipeline(patch):
                raise RuntimeError("采购卡带派发设置失败")
            mfaalog.info(f"[②采购] 实际清单已生成：{len(table)}张卡带，需更新收藏{len(pending)}张")
            for cartridge in pending:
                mfaalog.info(f"[②采购] {cartridge}：更新收藏 → {', '.join(sorted(table[cartridge])) or '清空收藏'}")
            return True
        except Exception as exc:
            clear_purchase_run(argv.task_detail.task_id)
            mfaalog.error(f"[②采购] 清单准备失败：{exc}")
            return False


@AgentServer.custom_action("ArbitrageBuyScanPrepare")
class ArbitrageBuyScanPrepare(CustomAction):
    def run(self, context, argv):
        task_id = argv.task_detail.task_id
        try:
            if not sync_from_context(context, where="ArbitrageBuyScanPrepare"):
                raise ValueError("全扫准备时存档号不可用")
            run = get_purchase_run(task_id)
            if run is None:
                raise ValueError("全扫准备缺少本轮采购名单")
            catalog = load_purchase_catalog()
            # 整轮周标记只代表扫描尝试已结束；未找到的卡带也必须失去旧成功清单。
            if not store.invalidate_purchase_alignments(run["table"]):
                raise RuntimeError("全扫前撤销旧收藏记录失败")
            run["pending"] = set(run["table"])
            patch = {definition["selector"]: {"enabled": cartridge in run["pending"]}
                     for cartridge, definition in catalog.items()}
            if not context.override_pipeline(patch):
                raise RuntimeError("采购全扫卡带派发设置失败")
            mfaalog.info(f"[②采购] 周期入口要求全扫：{len(run['pending'])}张卡带；成功核实后逐卡保存")
            return True
        except Exception as exc:
            clear_purchase_run(task_id)
            mfaalog.error(f"[②采购] 全扫准备失败：{exc}")
            return False


@AgentServer.custom_recognition("ArbitrageBuyNeedsScan")
class ArbitrageBuyNeedsScan(CustomRecognition):
    def analyze(self, context, argv):
        try:
            run = get_purchase_run(argv.task_detail.task_id)
            if run and run["pending"]:
                return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"pending": sorted(run["pending"])})
        except Exception as exc:
            mfaalog.error(f"[②采购] 收藏更新判定失败：{exc}")
        return None


@AgentServer.custom_recognition("ArbitrageBuyScanFinished")
class ArbitrageBuyScanFinished(CustomRecognition):
    def analyze(self, context, argv):
        try:
            run = get_purchase_run(argv.task_detail.task_id)
            if run is not None and not run["pending"]:
                return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"finished": True})
        except Exception as exc:
            mfaalog.error(f"[②采购] 收藏扫描收尾检查失败：{exc}")
        return None


@AgentServer.custom_action("ArbitrageBuyReady")
class ArbitrageBuyReady(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="ArbitrageBuyReady"):
                return False
            run = get_purchase_run(argv.task_detail.task_id)
            if run is None:
                mfaalog.warning("[②采购] 本轮采购名单未准备成功，结束常规采购")
                return False
            unverified = sorted(set(run["failed_cards"]) | run["pending"])
            if unverified:
                mfaalog.warning(f"[②采购] 收藏未核实卡带：{unverified}；继续按游戏当前收藏一键购买")
            else:
                mfaalog.info("[②采购] 收藏核实完成，继续一键购买")
            return True
        except Exception as exc:
            mfaalog.error(f"[②采购] 购买放行检查失败：{exc}")
            return False


@AgentServer.custom_action("ArbitrageBuyComplete")
class ArbitrageBuyComplete(CustomAction):
    def run(self, context, argv):
        try:
            if not sync_from_context(context, where="ArbitrageBuyComplete"):
                return False
            run = get_purchase_run(argv.task_detail.task_id)
            if run is None:
                raise ValueError("常规采购完成时缺少本轮最终名单")
            # 本节点只由一键购买的金币确认出口进入；收藏对齐本身不证明已买。
            run["completed_day"] = store.market_day()
            return True
        except Exception as exc:
            mfaalog.error(f"[②采购] 完成记录失败：{exc}")
            return False
