"""Maa adapter for the weekly stealing scheduler; account storage stays shared."""

import json

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from utils import mfaalog
from utils.account_sync import sync_from_context

from .cartridge_lib import manager
from .return_teleport import ReturnTeleportRunner, ReturnTeleportUnavailable
from .steal_navigation import StealNavigator, navigation_overrides
from .steal_schedule import WeeklyRunner, completion_params, load_registry, log_report, select_routes


CARD_AREA_NAMES = {
    1: ("卢戈", "盧戈"),
    2: ("达雷普", "達雷普"),
    3: ("布伦", "布倫"),
    4: ("凯洛", "凱洛"),
    5: ("亚拉里克", "亞拉里克"),
    6: ("施塔因之塔",),
    7: ("拉勒凯", "拉勒凱"),
    8: ("罗戴夫", "羅戴夫"),
}


def _cleanup_teleport_menu(runner, logger):
    cleanup = getattr(runner, "close_known_menu", None)
    capture = getattr(runner, "capture", None)
    if not cleanup or not capture:
        return
    try:
        cleanup(capture())
    except Exception as exc:
        logger.error(f"[偷窃周常] 清理传送阵界面失败：{exc}")


def _should_exit_indoor_before_retry(exc):
    return "未配置生成魔法阵" not in str(exc)


def _navigate_to_outdoor_board(context, card):
    params = {
        "card": card,
        "destination": "board",
        "expected_npc_names": ["探索告示板"],
        "area_names": list(CARD_AREA_NAMES[card]),
    }
    local = context.clone()
    if not local.override_pipeline(navigation_overrides(params)):
        raise RuntimeError("无法配置室外探索告示板导航节点")
    navigator = StealNavigator(local, params)
    if not navigator.run() or not navigator.confirmed_npc:
        raise RuntimeError(f"未能快速移动至剧情卡{card}室外探索告示板")


def return_card_to_portal(context, card, *, runner_factory=ReturnTeleportRunner,
                          navigate_outdoor=_navigate_to_outdoor_board, logger=mfaalog):
    if card not in CARD_AREA_NAMES:
        raise ValueError(f"不支持的剧情卡编号：{card}")
    runner = runner_factory(context.clone(), skip_if_at_portal=False)
    try:
        if not runner.run():
            raise ReturnTeleportUnavailable("未确认回到传送阵")
        return runner.result or "restored"
    except ReturnTeleportUnavailable as exc:
        _cleanup_teleport_menu(runner, logger)
        if context.tasker.stopping or not _should_exit_indoor_before_retry(exc):
            raise
        logger.info(
            f"[偷窃周常] 剧情卡{card} 当前位置无法直接使用传送技能（{exc}），"
            "先前往室外探索告示板后再次回到传送阵。"
        )
    navigate_outdoor(context, card)
    if context.tasker.stopping:
        raise RuntimeError("任务已停止")
    retry_runner = runner_factory(context.clone(), skip_if_at_portal=False)
    try:
        if not retry_runner.run():
            raise ReturnTeleportUnavailable("前往探索告示板后仍未确认回到传送阵")
        return retry_runner.result or "restored"
    except ReturnTeleportUnavailable:
        _cleanup_teleport_menu(retry_runner, logger)
        raise


@AgentServer.custom_action("StealWeekly")
class StealWeeklyAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            raw = argv.custom_action_param
            params = json.loads(raw) if isinstance(raw, str) else raw
            routes = select_routes(params, load_registry())
            if context.tasker.stopping:
                mfaalog.info("[偷窃周常] 任务已停止，没有启动路线。")
                return False
            if not sync_from_context(context, where="StealWeekly"):
                mfaalog.error("[偷窃周常] 账号存档未绑定，未执行任何路线。")
                return False

            def completed(route):
                if not sync_from_context(context, where="StealWeekly/check"):
                    raise RuntimeError("账号存档绑定失效")
                return manager.check_availability(completion_params(route))

            def return_to_portal(card):
                return return_card_to_portal(context, card)

            report = WeeklyRunner(context, completed, mfaalog, card_return=return_to_portal).run(
                routes, skip_completed=params.get("skip_completed", True))
            log_report(report, mfaalog)
            return report.succeeded
        except Exception as exc:
            mfaalog.error(f"[偷窃周常] 调度失败：{exc}；任务未完成。")
            return False
