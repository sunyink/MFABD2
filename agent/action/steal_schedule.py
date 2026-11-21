"""Pure scheduling logic for the thirteen weekly stealing targets.

This module does not read account saves or send input on its own. The host action
supplies the existing account-aware completion check and the Maa task context.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


ROUTE_IDS = (
    "C1_CuriousYouth", "C1_Collector", "C2_LoanShark",
    "C3_Mercenary", "C3_Shopkeeper", "C4_CoolWindYouth", "C4_TavernGuest",
    "C5_Merchant", "C5_Lover", "C6_Apprentice", "C6_Stone",
    "C7_Boatman", "C8_TaxCollector",
)
REGISTRY_PATH = Path(__file__).with_name("steal_registry.json")
NODE_NAME = re.compile(r"^Steal_[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class Route:
    route_id: str
    name: str
    card: int
    completion_key: str
    entry: str | None
    terminal: str | None
    mark_node: str | None
    live_verified: bool


@dataclass(frozen=True)
class Outcome:
    route_id: str
    name: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class CardReturnOutcome:
    card: int
    status: str
    reason: str = ""


@dataclass(frozen=True)
class RunReport:
    outcomes: tuple[Outcome, ...]
    stopped: bool = False
    return_outcomes: tuple[CardReturnOutcome, ...] = ()

    @property
    def return_failed(self):
        return tuple(item for item in self.return_outcomes
                     if item.status not in ("restored", "already"))

    @property
    def succeeded(self):
        return (bool(self.outcomes) and not self.stopped
                and all(item.status in ("completed", "skipped") for item in self.outcomes)
                and not self.return_failed)

    @property
    def unfinished(self):
        return tuple(item for item in self.outcomes if item.status not in ("completed", "skipped"))


def load_registry(path=REGISTRY_PATH):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("偷窃路线表版本无效")
    items = payload.get("routes")
    if not isinstance(items, list) or len(items) != len(ROUTE_IDS):
        raise ValueError("偷窃路线表必须包含13名目标")
    routes = {}
    fields = {"route_id", "name", "card", "completion_key", "entry", "terminal", "mark_node", "live_verified"}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("偷窃路线字段无效")
        route = Route(**item)
        if route.route_id not in ROUTE_IDS or route.route_id in routes:
            raise ValueError("偷窃路线ID缺失、未知或重复")
        if (not isinstance(route.name, str) or not route.name.strip()
                or type(route.card) is not int or not 1 <= route.card <= 8
                or type(route.live_verified) is not bool):
            raise ValueError("偷窃路线身份字段无效")
        if (not isinstance(route.completion_key, str)
                or not NODE_NAME.fullmatch(route.completion_key)
                or not route.completion_key.endswith("_3Characters")):
            raise ValueError("完成key必须专用于三个角色，不能复用旧单次记录")
        nodes = (route.entry, route.terminal, route.mark_node)
        if all(node is None for node in nodes):
            if route.live_verified:
                raise ValueError("缺少路线不能声明已实机验证")
        elif not all(isinstance(node, str) and NODE_NAME.fullmatch(node) for node in nodes):
            raise ValueError("路线入口、终点和完成节点必须同时配置")
        routes[route.route_id] = route
    if set(routes) != set(ROUTE_IDS):
        raise ValueError("偷窃路线表未覆盖全部13名目标")
    return {route_id: routes[route_id] for route_id in ROUTE_IDS}


def select_routes(params, registry):
    if not isinstance(params, dict) or set(params) - {"route_ids", "skip_completed"}:
        raise ValueError("StealWeekly只接受可选的route_ids和skip_completed参数")
    if type(params.get("skip_completed", True)) is not bool:
        raise ValueError("skip_completed必须为布尔值")
    route_ids = params.get("route_ids", list(ROUTE_IDS))
    if (not isinstance(route_ids, list) or not 1 <= len(route_ids) <= len(ROUTE_IDS)
            or any(not isinstance(route_id, str) or route_id not in registry for route_id in route_ids)
            or len(set(route_ids)) != len(route_ids)):
        raise ValueError("route_ids必须是1至13个不重复的已知路线ID")
    return tuple(registry[route_id] for route_id in route_ids)


def completion_params(route):
    return {
        "targets": [{"card_name": route.completion_key, "cycle_type": "steal_weekly"}],
        "match": "all_done",
    }


def result_confirms_route(result, route):
    if result is None or not result.status.succeeded:
        return False
    completed_nodes = {node.name for node in result.nodes if node.completed}
    return route.mark_node in completed_nodes and route.terminal in completed_nodes


def _mark_node_executed(result, route):
    if result is None:
        return False
    return any(node.name == route.mark_node and node.completed for node in getattr(result, "nodes", ()))


class WeeklyRunner:
    def __init__(self, context, completion_check, logger, card_return=None, *,
                 cycle_key=None, clear_completion=None):
        self.context = context
        self.completion_check = completion_check
        self.logger = logger
        self.card_return = card_return
        self.cycle_key = cycle_key
        self.clear_completion = clear_completion

    def _rollback_mark(self, route, reason):
        if self.clear_completion is None:
            return
        try:
            self.clear_completion(route)
            self.logger.info(f"[偷窃周常] {route.name}：{reason}，已撤销本次未确认的周完成记录。")
        except Exception as exc:
            self.logger.error(f"[偷窃周常] {route.name}：撤销周完成记录失败：{exc}")

    def stopping(self):
        return bool(self.context.tasker.stopping)

    def stopped_report(self, outcomes, remaining, started=False, return_outcomes=()):
        if started and remaining:
            route, remaining = remaining[0], remaining[1:]
            outcomes.append(Outcome(route.route_id, route.name, "interrupted", "执行期间收到停止请求，结果未确认"))
        outcomes.extend(Outcome(route.route_id, route.name, "not_run", "任务已停止") for route in remaining)
        return RunReport(tuple(outcomes), stopped=True, return_outcomes=tuple(return_outcomes))

    def _return_card_if_ready(self, card, card_statuses, return_outcomes, outcomes, remaining):
        if self.card_return is None:
            return None
        statuses = card_statuses.get(card, ())
        if not statuses or "completed" not in statuses:
            return None
        if not all(status in ("completed", "skipped") for status in statuses):
            return None
        if self.stopping():
            return_outcomes.append(CardReturnOutcome(card, "interrupted", "执行期间收到停止请求，未回到传送阵"))
            return self.stopped_report(outcomes, remaining, return_outcomes=return_outcomes)
        self.logger.info(f"[偷窃周常] 剧情卡{card} 目标已全部完成，开始使用传送功能回到传送阵。")
        try:
            status = self.card_return(card)
            if self.stopping():
                return_outcomes.append(CardReturnOutcome(card, "interrupted", "回传送阵期间收到停止请求，结果未确认"))
                return self.stopped_report(outcomes, remaining, return_outcomes=return_outcomes)
            if status not in ("restored", "already"):
                raise RuntimeError("未确认回到传送阵")
            return_outcomes.append(CardReturnOutcome(card, status))
            detail = "已在传送阵上" if status == "already" else "已传送回传送阵"
            self.logger.info(f"[偷窃周常] 剧情卡{card}：{detail}。")
        except Exception as exc:
            if self.stopping():
                return_outcomes.append(CardReturnOutcome(card, "interrupted", "回传送阵期间收到停止请求，结果未确认"))
                return self.stopped_report(outcomes, remaining, return_outcomes=return_outcomes)
            return_outcomes.append(CardReturnOutcome(card, "failed", str(exc)))
            tail = "继续下一张地图" if remaining else "本次任务结束"
            self.logger.error(f"[偷窃周常] 剧情卡{card} 回到传送阵失败：{exc}；{tail}。")
        return None

    def run(self, routes, *, skip_completed=True):
        if type(skip_completed) is not bool:
            raise ValueError("skip_completed必须为布尔值")
        if not skip_completed:
            self.logger.info("[偷窃周常] 已关闭按周记录跳过，将重新检查所选NPC的三个角色。")
        outcomes = []
        return_outcomes = []
        card_statuses = {}
        last_index_by_card = {}
        for index, route in enumerate(routes):
            last_index_by_card[route.card] = index
        for index, route in enumerate(routes):
            if self.stopping():
                return self.stopped_report(outcomes, routes[index:], return_outcomes=return_outcomes)
            if route.entry is None:
                outcomes.append(Outcome(route.route_id, route.name, "pending", "路线尚未实现"))
                card_statuses.setdefault(route.card, []).append("pending")
                self.logger.error(f"[偷窃周常] {route.name}：缺少路线，保留为未完成。")
            else:
                started = False
                try:
                    already_done = self.completion_check(route) if skip_completed else False
                    if self.stopping():
                        return self.stopped_report(outcomes, routes[index:], return_outcomes=return_outcomes)
                    if already_done:
                        outcomes.append(Outcome(route.route_id, route.name, "skipped"))
                        card_statuses.setdefault(route.card, []).append("skipped")
                        self.logger.info(f"[偷窃周常] {route.name}：本周三角色完成记录有效，跳过。")
                    else:
                        local = self.context.clone()
                        # FinalScene must pass before Mark writes the weekly record.
                        # The DirectHit terminal then ends this route, including when
                        # the calibration JSON links it to another route.
                        overrides = {route.terminal: {"next": [], "on_error": []}}
                        if self.stopping():
                            return self.stopped_report(outcomes, routes[index:], return_outcomes=return_outcomes)
                        if not route.live_verified:
                            self.logger.info(f"[偷窃周常] {route.name}：执行待实机验证路线，完成前逐项核验。")
                        start_cycle = self.cycle_key() if self.cycle_key is not None else None
                        started = True
                        result = local.run_task(route.entry, overrides)
                        end_cycle = self.cycle_key() if self.cycle_key is not None else None
                        cycle_changed = start_cycle is not None and end_cycle != start_cycle
                        confirmed = result_confirms_route(result, route)
                        if self.stopping():
                            if cycle_changed:
                                self._rollback_mark(route, "停止期间跨过周一08:00周刷新临界点")
                            elif skip_completed and not confirmed and _mark_node_executed(result, route):
                                self._rollback_mark(route, "停止前已写入但未确认结束节点")
                            return self.stopped_report(
                                outcomes, routes[index:], started=True, return_outcomes=return_outcomes)
                        if cycle_changed:
                            self._rollback_mark(route, "执行期间跨过周一08:00周刷新临界点")
                            raise RuntimeError("执行期间跨过周一08:00周刷新临界点，已撤销记录待重检")
                        if not confirmed:
                            if skip_completed and _mark_node_executed(result, route):
                                self._rollback_mark(route, "未通过结束节点校验")
                            raise RuntimeError("未通过独立路线的完成节点和结束场景校验")
                        persisted = self.completion_check(route)
                        end_cycle_after = self.cycle_key() if self.cycle_key is not None else None
                        if start_cycle is not None and end_cycle_after != start_cycle:
                            self._rollback_mark(route, "确认记录期间跨过周一08:00周刷新临界点")
                            raise RuntimeError("确认记录期间跨过周一08:00周刷新临界点，已撤销记录待重检")
                        if self.stopping():
                            return self.stopped_report(
                                outcomes, routes[index:], started=True, return_outcomes=return_outcomes)
                        if not persisted:
                            raise RuntimeError("路线已结束但三角色周记录未确认")
                        outcomes.append(Outcome(route.route_id, route.name, "completed"))
                        card_statuses.setdefault(route.card, []).append("completed")
                        self.logger.info(f"[偷窃周常] {route.name}：三角色完成及周记录均已确认。")
                except Exception as exc:
                    if self.stopping():
                        return self.stopped_report(
                            outcomes, routes[index:], started=started, return_outcomes=return_outcomes)
                    outcomes.append(Outcome(route.route_id, route.name, "failed", str(exc)))
                    card_statuses.setdefault(route.card, []).append("failed")
                    self.logger.error(f"[偷窃周常] {route.name}：{exc}；继续下一独立目标。")
            if index == last_index_by_card[route.card]:
                stopped = self._return_card_if_ready(
                    route.card, card_statuses, return_outcomes, outcomes, routes[index + 1:])
                if stopped is not None:
                    return stopped
        return RunReport(tuple(outcomes), return_outcomes=tuple(return_outcomes))


def log_report(report, logger):
    completed = sum(item.status == "completed" for item in report.outcomes)
    skipped = sum(item.status == "skipped" for item in report.outcomes)
    message = (f"[偷窃周常] 本次{len(report.outcomes)}名目标：本次确认{completed}，"
               f"按周记录跳过{skipped}，未完成{len(report.unfinished)}。")
    if report.succeeded:
        logger.info(message)
    else:
        logger.error(message)
        if report.unfinished:
            logger.error("[偷窃周常] 未完成：" + "；".join(
                f"{item.name}（{item.reason or item.status}）" for item in report.unfinished))
    if report.return_outcomes:
        returned = sum(item.status in ("restored", "already") for item in report.return_outcomes)
        return_message = (f"[偷窃周常] 地图回传送阵：{returned}/{len(report.return_outcomes)}张已确认，"
                          f"未确认{len(report.return_failed)}张。")
        if report.return_failed:
            logger.error(return_message)
            logger.error("[偷窃周常] 回传送阵未确认：" + "；".join(
                f"剧情卡{item.card}（{item.reason or item.status}）" for item in report.return_failed))
        else:
            logger.info(return_message)
    if report.stopped:
        logger.info("[偷窃周常] 已响应停止请求，没有启动后续路线。")
