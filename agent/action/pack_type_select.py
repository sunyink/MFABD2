import json

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import mfaalog
from utils.account_sync import sync_from_context
from utils.collect_config import card_in_debug_list, debug_list_numbers
from .cartridge_lib import manager

# ==============================================================================
# 目标选择器（Collect_PackType_Select）
# ==============================================================================
# 位于卡带名单维护链末端（屏蔽 → 指定，或调试直采）之后、调度器 Collect_QuickCart_Menu 之前。
# 对比「运行态启用的具名卡带」与本周完成记录，某类没有要采的卡带就关掉该类入口：
#   启用的卡带为空 -> 关；有卡在调试名单 -> 不关；全部本周已完成 -> 关；否则不关。
# 默认不支持采集与被名单关掉的卡带都是 enabled:false，这里一概按「不采」处理，不区分原因。
# 只关不开；任何一步判不出来就不关该类（宁可多扫，不可漏采）。
# 类别、卡带节点前缀与入口节点的对照写在节点的 custom_action_param.types 里。
CYCLE_TYPE = "g_weekly"
PACK_END_NODE = "Collect_PackEnd_Ck"


def _custom_action_params(node_data, action_name):
    """读取 V2 归一化节点或 V1 补丁里的指定 Custom 动作参数。"""
    if not isinstance(node_data, dict):
        return None
    action = node_data.get("action")
    if isinstance(action, dict):
        if action.get("type") != "Custom":
            return None
        param = action.get("param")
    else:
        if action != "Custom":
            return None
        param = node_data
    if not isinstance(param, dict) or param.get("custom_action") != action_name:
        return None
    value = param.get("custom_action_param")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def card_name_of(node_data):
    """从具名卡给成功出口设置的 MarkComplete 载荷读取卡名，不读取识别或存档。"""
    params = _custom_action_params(node_data, "PatchPipeline")
    if params is None:
        return None
    if "patches" in params:
        if "target" in params or "patch" in params:
            return None
        patches = params["patches"]
        payload = patches.get(PACK_END_NODE) if isinstance(patches, dict) else None
    else:
        targets = params.get("target")
        if targets != PACK_END_NODE and not (isinstance(targets, list) and PACK_END_NODE in targets):
            return None
        payload = params.get("patch")
    completion = _custom_action_params(payload, "MarkComplete")
    card = completion.get("card_name") if completion else None
    return card if isinstance(card, str) and card.strip() else None


def decide(cards, in_debug, all_done):
    """一类的判定 -> (是否关闭, 原因)。in_debug(card) / all_done(cards) 由调用方注入。"""
    if not cards:
        return True, "没有启用的卡带"
    if any(in_debug(c) for c in cards):
        return False, "调试名单内有卡带"
    if all_done(cards):
        return True, f"启用 {len(cards)} 张，本周全部已完成"
    return False, f"启用 {len(cards)} 张，仍有未完成"


@AgentServer.custom_action("PackTypeSelect")
class PackTypeSelect(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        if not sync_from_context(context, where="PackTypeSelect"):
            return CustomAction.RunResult(success=False)
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw) or "{}")
            type_list = params.get("types") if isinstance(params, dict) else None
            if not isinstance(type_list, list) or not type_list:
                raise ValueError(f"types 必须是非空数组，实际为 {type_list!r}")
            names = [str(n) for n in context.tasker.resource.node_list]
        except Exception as e:
            mfaalog.error(f"[目标选择] 参数或节点表读取失败，本次不关闭任何类别: {e}")
            return CustomAction.RunResult(success=True)

        debug_map = debug_list_numbers(context)

        def all_done(cards):
            return manager.check_availability({
                "targets": [{"card_name": c, "cycle_type": CYCLE_TYPE} for c in cards],
                "match": "all_done",
            })

        patches = {}
        for t in type_list:
            if not isinstance(t, dict):
                mfaalog.error(f"[目标选择] 类别配置不是对象: {t!r}")
                continue
            label = t.get("label") or t.get("switch")
            prefix, switch = t.get("pack_prefix"), t.get("switch")
            if not prefix or not switch:
                mfaalog.error(f"[目标选择] 类别配置缺少 pack_prefix 或 switch: {t}")
                continue
            cards, unknown = [], []
            for nm in names:
                if not (nm.startswith(prefix) and nm[len(prefix):].isdigit()):
                    continue
                try:
                    data = context.get_node_data(nm)
                except Exception:
                    data = None
                if isinstance(data, dict) and data.get("enabled") is False:
                    continue
                card = card_name_of(data) if isinstance(data, dict) else None
                if card:
                    cards.append(card)
                else:
                    unknown.append(nm)
            if unknown:
                mfaalog.error(f"[目标选择] {label}：保留｜{unknown} 读不到卡名，无法判断")
                continue
            close, reason = decide(cards, lambda c: card_in_debug_list(c, debug_map), all_done)
            if close:
                patches[switch] = {"enabled": False}
            mfaalog.info(f"[目标选择] {label}：{'关闭' if close else '保留'}｜{reason}")

        if patches:
            try:
                context.override_pipeline(patches)
            except Exception as e:
                mfaalog.error(f"[目标选择] override_pipeline 失败，本次不关闭任何类别: {e}")
        return CustomAction.RunResult(success=True)
