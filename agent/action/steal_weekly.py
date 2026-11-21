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


def _is_stop_or_dialogue_error(context, exc):
    if getattr(getattr(context, "tasker", None), "stopping", False):
        return True
    message = str(exc)
    return "任务已停止" in message or "剧情对话" in message


def _current_steal_cycle():
    calc = getattr(manager, "_calculate_server_reset_timestamp", None)
    if calc is None:
        return None
    reset_ts, _ = calc("steal_weekly")
    return reset_ts


def _clear_steal_completion(context, route):
    if not sync_from_context(context, where="StealWeekly/rollback"):
        raise RuntimeError("账号存档绑定失效")
    from utils.persistent_store import PersistentStore
    key_fn = getattr(manager, "_get_storage_key", None)
    storage_key = (
        key_fn(route.completion_key, "steal_weekly")
        if key_fn is not None
        else f"{route.completion_key}@steal_weekly"
    )
    store_key = getattr(manager, "STORE_KEY", "cycles")
    data = PersistentStore.load()
    marks = data.get(store_key)
    if isinstance(marks, dict) and storage_key in marks:
        del marks[storage_key]
        if not PersistentStore.save(data):
            raise RuntimeError("撤销偷窃完成记录保存失败")


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
        if not _is_stop_or_dialogue_error(context, exc):
            _cleanup_teleport_menu(runner, logger)
        if context.tasker.stopping or not _should_exit_indoor_before_retry(exc):
            raise
        logger.info(
            f"[偷窃周常] 剧情卡{card} 当前位置无法直接使用传送技能（{exc}），"
            "先前往室外探索告示板后再次回到传送阵。"
        )
    except Exception as exc:
        if not _is_stop_or_dialogue_error(context, exc):
            _cleanup_teleport_menu(runner, logger)
        raise
    navigate_outdoor(context, card)
    if context.tasker.stopping:
        raise RuntimeError("任务已停止")
    retry_runner = runner_factory(context.clone(), skip_if_at_portal=False)
    try:
        if not retry_runner.run():
            raise ReturnTeleportUnavailable("前往探索告示板后仍未确认回到传送阵")
        return retry_runner.result or "restored"
    except ReturnTeleportUnavailable as exc:
        if not _is_stop_or_dialogue_error(context, exc):
            _cleanup_teleport_menu(retry_runner, logger)
        raise
    except Exception as exc:
        if not _is_stop_or_dialogue_error(context, exc):
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

            def clear_completion(route):
                _clear_steal_completion(context, route)

            try:
                runner = WeeklyRunner(
                    context,
                    completed,
                    mfaalog,
                    card_return=return_to_portal,
                    cycle_key=_current_steal_cycle,
                    clear_completion=clear_completion,
                )
            except TypeError:
                runner = WeeklyRunner(context, completed, mfaalog, card_return=return_to_portal)
            report = runner.run(routes, skip_completed=params.get("skip_completed", True))
            log_report(report, mfaalog)
            return report.succeeded
        except Exception as exc:
            mfaalog.error(f"[偷窃周常] 调度失败：{exc}；任务未完成。")
            return False
