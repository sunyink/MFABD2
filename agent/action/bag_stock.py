"""背包补齐本轮料理未留下有效读数的食材；找图后以详情名称和数量为准。"""

from pathlib import PurePosixPath
import json
import time

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .cooking_stock import _clean, _read_texts, _unique_parsed, _NUMBER, get_cooking_scan_start
from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.arbitrage_store import (
    get_inventory_items, invalidate_inventory_quantities,
    set_inventory_quantities, save_bag_stock_summary, utc_now,
)
from utils.name_i18n import canon


_REASON_TEXT = {
    "detail": "详情数量已核实",
    "ingredient_boundary": "已到食材末端，确认没有",
    "detail_unreadable": "图标命中，但详情未核实",
    "page_limit": "达到翻页上限，仍未确认",
}


def bag_catalog(context, config) -> dict[str, str]:
    source = context.get_node_object(config["catalog_node"]).attach["templates"]
    return {canon(name): str(PurePosixPath(config["template_dir"]) / PurePosixPath(path).name)
            for name, path in source.items()}


def pending_materials(catalog: dict, items: dict, started_at: str | None) -> list[str]:
    """只复用本轮观测且未被后续制作作废的精确量；单独运行背包则全部重查。"""
    pending = []
    for name in catalog:
        fact = items.get(name, {})
        quantity = fact.get("quantity")
        if not (started_at and fact.get("quantity_status") == "known"
                and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0
                and fact.get("quantity_observed_at", "") > started_at):
            pending.append(name)
    return pending


class BagScanner:
    def __init__(self, context, config):
        self.context = context
        self.config = config
        self.deadline = time.monotonic() + config["timeout_seconds"]
        self.pages = 0

    def check_running(self):
        if self.context.tasker.stopping:
            raise RuntimeError("任务已停止")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("背包补查超过总时限")

    def capture(self):
        self.check_running()
        image = self.context.tasker.controller.post_screencap().wait().get()
        if image is None or not image.size:
            raise RuntimeError("背包截图失败")
        return image

    def recognize(self, node, image, override=None):
        result = self.context.run_recognition(node, image, override or {})
        if result is None:
            raise RuntimeError(f"识别未执行: {node}")
        return result

    def action(self, node, box=(0, 0, 0, 0)):
        self.check_running()
        result = self.context.run_action(node, box=box)
        if result is None or not result.success:
            raise RuntimeError(f"动作失败: {node}")

    def list_image(self):
        image = self.capture()
        if not self.recognize(self.config["list_node"], image).hit:
            raise RuntimeError("未确认料理食材排序的消耗品列表，停止补查")
        return image

    def reset(self):
        self.check_running()
        result = self.context.run_task(self.config["reset_node"])
        # 项目默认 on_error 可使子任务静默结束，必须同时看到真正的出口。
        if result is None or not any(node.name == self.config["reset_exit"] for node in result.nodes):
            raise RuntimeError("背包页签复位未完成")
        return self.list_image()

    def inspect(self, name, box):
        self.action(self.config["click_node"], box)
        try:
            for attempt in range(2):
                image = self.capture()
                names, _, _ = _read_texts(self.context, self.config["name_node"], image)
                if {canon(_clean(text)) for text in names} != {name}:
                    continue
                texts, _, _ = _read_texts(self.context, self.config["quantity_node"], image,
                                         retry=bool(attempt))
                quantity = _unique_parsed(texts, r"[拥擁]有" + _NUMBER + r"[个個]")
                if quantity is not None:
                    return quantity[0]
            return None
        finally:
            # 只调用已实测出口的动作；其排序菜单 OCR 不适用于物品详情。
            if not self.context.tasker.stopping:
                self.action(self.config["close_node"])
                self.list_image()

    def search_page(self, name, template, image):
        node = self.config["template_node"]
        result = self.recognize(node, image, {node: {"template": [template]}})
        if not result.hit:
            return None, False
        matches = getattr(result, "filtered_results", None) or []
        boxes = [item.box for item in matches] or [result.box]
        for box in boxes:
            quantity = self.inspect(name, box)
            if quantity is not None:
                return quantity, True
        # 模板命中过而详情未核实，不能在终点把疑似漏读的材料写成零。
        return None, True

    def find(self, name, template):
        image = self.list_image()
        quantity, uncertain = self.search_page(name, template, image)
        if quantity is not None:
            return quantity, "detail"
        image = self.reset()
        for page in range(self.config["max_pages_per_item"]):
            self.pages += 1
            quantity, suspect = self.search_page(name, template, image)
            uncertain |= suspect
            if quantity is not None:
                return quantity, "detail"
            # 同一页先查目标，再查终点；不遗漏与成长材料同屏的最后几行食材。
            if self.recognize(self.config["end_node"], image).hit:
                return (None, "detail_unreadable") if uncertain else (0, "ingredient_boundary")
            if page + 1 < self.config["max_pages_per_item"]:
                self.action(self.config["swipe_node"])
                image = self.list_image()
        return None, "page_limit"


@AgentServer.custom_action("BagStockScan")
class BagStockScan(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        if not sync_from_context(context, where="BagStockScan"):
            return False
        record = {"observed_at": utc_now(), "complete": False, "read_item_names": [],
                  "absent_item_names": [], "unknown_item_names": [], "reused_item_names": [],
                  "errors": {}, "pages_scanned": 0}
        scanner = None
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw))
            config = context.get_node_object(params["config_node"]).attach
            catalog = bag_catalog(context, config)
            pending = pending_materials(catalog, get_inventory_items(),
                                        get_cooking_scan_start(argv.task_detail.task_id))
            record["unknown_item_names"] = list(pending)
            record["reused_item_names"] = [name for name in catalog if name not in pending]
            if not invalidate_inventory_quantities(pending, "bag_scan_pending"):
                raise RuntimeError("待补查库存未能标记为未知")
            scanner = BagScanner(context, config)
            scanner.list_image()
            mfaalog.info(f"[BagStock] 复用本轮有效读数 {len(catalog) - len(pending)} 项，背包补查 {len(pending)} 项")
            for name in pending:
                quantity, reason = scanner.find(name, catalog[name])
                if quantity is None:
                    record["errors"][name] = reason
                    mfaalog.warning(f"[BagStock] {name}: 未知 ({_REASON_TEXT.get(reason, reason)})")
                    continue
                if not set_inventory_quantities({name: quantity}, "bag_detail" if reason == "detail"
                                                else "bag_absent", {"evidence": reason}):
                    raise RuntimeError(f"{name} 数量写入失败")
                record["unknown_item_names"].remove(name)
                key = "absent_item_names" if reason == "ingredient_boundary" else "read_item_names"
                record[key].append(name)
                mfaalog.info(f"[BagStock] {name}: {quantity} ({_REASON_TEXT.get(reason, reason)})")
            record["complete"] = not record["unknown_item_names"]
        except Exception as exc:
            record["errors"]["scan"] = str(exc)
            mfaalog.warning(f"[BagStock] 补查提前结束: {exc}")
        finally:
            record["pages_scanned"] = scanner.pages if scanner else 0
            try:
                if not save_bag_stock_summary(record):
                    mfaalog.error("[BagStock] 补查汇总写入失败")
            except Exception as exc:
                mfaalog.error(f"[BagStock] 补查汇总存档异常: {exc}")
        mfaalog.info(f"[BagStock] 已读 {len(record['read_item_names'])}，确认没有 {len(record['absent_item_names'])}，"
                     f"仍未知 {len(record['unknown_item_names'])}，完整={record['complete']}")
        # 允许出口复位并继续套利；业务是否完整只看持久化汇总 complete。
        return True
