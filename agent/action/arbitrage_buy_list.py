"""采购名单与扫描数据准备；扫描周期检查和打标由 Pipeline 负责。"""

import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition

from utils import arbitrage_store as store, mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_purchase_lists import (
    DATA_NODE, PREPARE_NODE, load_purchase_catalog, resolve_purchase_table,
    changed_cartridges, put_purchase_run, get_purchase_run, clear_purchase_run, node_enabled,
)
from utils.name_i18n import canon


def _record_run(context, task_id, where):
    """只在记录边界同步存档；失败后不让本轮继续使用原存档依据。"""
    if not sync_from_context(context, where=where):
        clear_purchase_run(task_id)
        return None
    return get_purchase_run(task_id)


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
            read_errors = {}
            if config["custom_enabled"]:
                for cartridge, definition in catalog.items():
                    try:
                        node = definition["custom_node"]
                        if node_enabled(context, node):
                            overrides[cartridge] = context.get_node_object(node).attach
                    except Exception as exc:
                        read_errors[cartridge] = str(exc)
            table, errors = resolve_purchase_table(defaults, catalog, overrides)
            errors.update(read_errors)
            for cartridge, reason in errors.items():
                table.pop(cartridge, None)
                mfaalog.warning(f"[②采购] {cartridge}名单不可用：{reason}；保留当前收藏")
            if not table:
                raise ValueError("没有可用的卡带采购名单")
            pending = changed_cartridges(table, store.get_purchase_alignments())
            put_purchase_run(argv.task_detail.task_id, table, pending, errors)
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
        run = None
        try:
            run = _record_run(context, task_id, "ArbitrageBuyScanPrepare")
            if run is None:
                raise ValueError("全扫准备缺少本轮采购名单")
            catalog = load_purchase_catalog()
            run["pending"] = set(run["table"])
            # 整轮周标记只代表扫描尝试已结束；未找到的卡带也必须失去旧成功清单。
            if not store.invalidate_purchase_alignments(run["table"]):
                raise RuntimeError("全扫前撤销旧收藏记录失败")
            patch = {definition["selector"]: {"enabled": cartridge in run["pending"]}
                     for cartridge, definition in catalog.items()}
            if not context.override_pipeline(patch):
                raise RuntimeError("采购全扫卡带派发设置失败")
            mfaalog.info(f"[②采购] 周期入口要求全扫：{len(run['pending'])}张卡带；成功核实后逐卡保存")
            return True
        except Exception as exc:
            if run is not None:
                run["scan_error"] = str(exc)
            mfaalog.error(f"[②采购] 全扫准备失败：{exc}")
            return False


@AgentServer.custom_recognition("ArbitrageBuyRecordReady")
class ArbitrageBuyRecordReady(CustomRecognition):
    def analyze(self, context, argv):
        try:
            run = _record_run(context, argv.task_detail.task_id, "ArbitrageBuyRecordReady")
            if run is not None and not run["scan_error"]:
                return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={"record_ready": True})
        except Exception as exc:
            mfaalog.warning(f"[②采购] 存档依据不可用，跳过扫描周记录：{exc}")
        return None


@AgentServer.custom_recognition("ArbitrageBuyNeedsScan")
class ArbitrageBuyNeedsScan(CustomRecognition):
    def analyze(self, context, argv):
        try:
            run = get_purchase_run(argv.task_detail.task_id)
            if run and not run["scan_error"] and run["pending"]:
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
            run = _record_run(context, argv.task_detail.task_id, "ArbitrageBuyReady")
            if run is None:
                mfaalog.warning("[②采购] 本轮采购名单或存档依据不可用")
                return False
            if run["scan_error"]:
                mfaalog.warning(f"[②采购] 收藏扫描准备未完成：{run['scan_error']}")
                return False
            unverified = sorted(set(run["failed_cards"]) | run["pending"])
            if unverified:
                mfaalog.warning(f"[②采购] 收藏未核实卡带：{unverified}")
                return False
            else:
                mfaalog.info("[②采购] 收藏核实完成，继续一键购买")
            return True
        except Exception as exc:
            mfaalog.error(f"[②采购] 购买放行检查失败：{exc}")
            return False


# 一键购买怎么收的尾 →（登记成交日期，作废库存旧数）。
_OUTCOMES = {"bought": (True, True), "empty": (True, False), "unconfirmed": (False, True)}


def _purchasable_names(run) -> list:
    """一键购买可能买进的商品：名单上的全部，收藏没核上、名单读不出的卡带货架上的全部；没有名单就是整个目录。"""
    catalog = load_purchase_catalog()
    if run is None:
        return sorted({canon(name) for row in catalog.values() for name in row["items"]})
    names = {name for items in run["table"].values() for name in items}
    for cartridge in run["pending"] | run["failed_cards"].keys():
        names.update(canon(name) for name in catalog[cartridge]["items"])
    return sorted(names)


@AgentServer.custom_action("ArbitrageBuyComplete")
class ArbitrageBuyComplete(CustomAction):
    """② 一键购买收尾，result 说明怎么收的尾（U63）。

    bought：金币确认扣款；empty：无商品提示，今天的收藏已买过；unconfirmed：点了确认，扣款没核上。
    前两种登记成交日期，供补买排除已购供给；可能买进的两种作废这些商品的库存旧数，
    ⑤ 见到未知就去柜台现读，不拿买前的数跳过柜台。不管收藏勾没勾成，按名单全部作废。
    """

    def run(self, context, argv):
        task_id = argv.task_detail.task_id
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            result = str(params.get("result") if isinstance(params, dict) else None)
            record, invalidate = _OUTCOMES[result]
        except Exception as exc:
            mfaalog.error(f"[②采购] 收尾参数无效({exc})，按可能买进处理、不登记成交")
            result, record, invalidate = "unknown", False, True
        try:
            if not sync_from_context(context, where="ArbitrageBuyComplete"):
                clear_purchase_run(task_id)
                mfaalog.warning("[②采购] 存档未同步，成交依据与库存作废都没写；继续收尾")
                return True
            try:
                run = get_purchase_run(task_id)
            except ValueError as exc:
                mfaalog.warning(f"[②采购] {exc}")
                run = None
        except Exception as exc:
            mfaalog.warning(f"[②采购] 存档读不到：{exc}；继续收尾")
            return True
        if invalidate:
            try:
                names = _purchasable_names(run)
                if not store.invalidate_inventory_quantities(names, f"purchase_{result}", {"task_id": task_id}):
                    raise RuntimeError("写入失败")
                scope = "整个商店目录" if run is None else "名单上"
                mfaalog.info(f"[②采购] {scope} {len(names)} 样商品的库存旧数已作废，⑤ 到柜台现读")
            except Exception as exc:
                mfaalog.error(f"[②采购] 库存旧数未能作废：{exc}；继续收尾")
        if record:
            try:
                if run is None:
                    raise ValueError("常规采购完成时缺少本轮最终名单")
                run["completed_day"] = store.market_day()
            except Exception as exc:
                mfaalog.warning(f"[②采购] 成交依据未保存：{exc}；继续收尾")
        return True
