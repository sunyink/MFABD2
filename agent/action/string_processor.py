import json
import re
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer
from utils import mfaalog
from utils.num_list import parse_number_list

@AgentServer.custom_action("BatchNumericPatch")
class BatchNumericPatch(CustomAction):
    """
    [多规则引擎版 - 修正版]
    修复了返回值类型导致的 Action.Failed
    """
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            mfaalog.debug("[BatchPatch] Engine 启动...")

            # ================= 🔧 开发者配置区域 🔧 =================
            SPLIT_PATTERN = r'[，,;\s|]+'
            RANGE_CONNECTORS = ['~', '～', '-']
            # =======================================================

            # 1. 解析参数
            try:
                # 兼容性处理：如果传进来已经是dict (某些特定版本行为)，直接用；如果是str则解析
                if isinstance(argv.custom_action_param, dict):
                    top_params = argv.custom_action_param
                elif not argv.custom_action_param:
                    # 空参数直接返回成功
                    return CustomAction.RunResult(success=True)
                else:
                    top_params = json.loads(argv.custom_action_param)
            except json.JSONDecodeError:
                mfaalog.error("[BatchPatch] 批量修改未执行：参数格式错误")
                mfaalog.debug(f"[BatchPatch] JSON 格式错误: {argv.custom_action_param}")
                return CustomAction.RunResult(success=True)

            # 2. 构建规则队列
            rules_to_process = []
            if isinstance(top_params, dict) and "rule_list" in top_params and isinstance(top_params["rule_list"], list): # type: ignore
                rules_to_process = top_params["rule_list"] # type: ignore
            else:
                rules_to_process = [top_params]

            # 获取公共上下文
            # [关键] 这里必须是 task.json 那个节点的 Key，否则读不到 attach
            current_node_name = top_params.get("node_name", "") if isinstance(top_params, dict) else ""
            node_attach_data = {}
            
            if current_node_name:
                node_obj = context.get_node_object(current_node_name)
                if node_obj and node_obj.attach:
                    node_attach_data = node_obj.attach
                else:
                    mfaalog.debug(f"[BatchPatch] 未找到节点对象或Attach为空: {current_node_name}")
            else:
                mfaalog.debug("[BatchPatch] node_name 未配置，将无法读取 Attach")

            final_override_dict = {}

            # 3. 遍历执行规则
            for index, rule in enumerate(rules_to_process):
                # [新增] 类型防御：如果 rule 不是字典（比如是字符串或其他乱七八糟的），直接跳过
                # 这行代码能消除编译器的 "str没有get方法" 警告
                if not isinstance(rule, dict):
                    mfaalog.warning("[BatchPatch] 一项修改规则格式无效，已跳过")
                    mfaalog.debug(f"[BatchPatch] 规则格式错误（非字典），跳过: {rule}")
                    continue

                # 下面的代码就安全了，Pylance 知道 rule 肯定是 dict
                rule_tag = rule.get("comment", f"Rule_{index}")
                
                prefix = rule.get("pre_string", "")
                suffix = rule.get("post_string", "")
                patch_content = rule.get("patch", {})
                attach_key = rule.get("attach_key", "input_string")
                
                # --- 合并逻辑 ---
                static_input = str(rule.get("input_string", "")).strip()
                dynamic_input = ""
                
                # 尝试从 attach 获取
                if attach_key in node_attach_data:
                    val = node_attach_data[attach_key]
                    if val is not None:
                        dynamic_input = str(val).strip()
                        mfaalog.debug(f"[BatchPatch] [{rule_tag}] 捕获 Attach 参数: {dynamic_input}")
                
                raw_input_str = f"{static_input},{dynamic_input}"
                
                if not raw_input_str.strip(",; \t\n"):
                    continue

                # --- 解析逻辑 ---
                tokens = [x for x in re.split(SPLIT_PATTERN, raw_input_str) if x]
                final_number_set = set()
                
                for token in tokens:
                    matched_connector = None
                    for conn in RANGE_CONNECTORS:
                        if conn in token:
                            matched_connector = conn
                            break
                    
                    if matched_connector:
                        try:
                            parts = token.split(matched_connector)
                            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                                start = int(parts[0].strip())
                                end = int(parts[1].strip())
                                if start > end:
                                    start, end = end, start
                                for i in range(start, end + 1):
                                    final_number_set.add(i)
                                    
                            else:
                                mfaalog.warning("[BatchPatch] 一项编号范围格式无效，已跳过")
                                mfaalog.debug(f"[BatchPatch] 范围格式无效: '{token}'")
                        except (ValueError, IndexError) as e:
                            # 捕获具体的转换错误，不吞没其他逻辑错误
                            mfaalog.warning("[BatchPatch] 一项编号范围无法解析，已跳过")
                            mfaalog.debug(f"[BatchPatch] 解析范围出错 '{token}': {e}")

                    else:
                        try:
                            final_number_set.add(int(token))
                        except ValueError:
                            final_number_set.add(token)

                if not final_number_set:
                    continue

                # --- 注入逻辑 ---
                item_list = list(final_number_set)
                try:
                    item_list.sort(key=lambda x: int(x) if isinstance(x, int) or (isinstance(x, str) and x.isdigit()) else str(x))
                except Exception as e:
                    # 只有当包含无法比较的类型时才会进这里（智能排序失败）
                    # 记录一条 Debug 日志即可，因为这可能是合法的纯文本输入
                    mfaalog.debug(f"[BatchPatch] 智能排序失败 (输入可能包含非数字)，回退到字符串排序: {e}")
                    item_list.sort(key=str)

                for item in item_list:
                    full_node_name = f"{prefix}{item}{suffix}"
                    final_override_dict[full_node_name] = patch_content

            # 4. 提交
            if final_override_dict:
                context.override_pipeline(final_override_dict)
                mfaalog.debug(f"[BatchPatch] 执行完毕，注入 {len(final_override_dict)} 个节点")
            
            # [修正] 必须返回 CustomAction.RunResult 对象
            return CustomAction.RunResult(success=True)

        except Exception as e:
            mfaalog.error("[BatchPatch] 批量修改执行失败")
            import traceback
            mfaalog.debug(f"[BatchPatch] 致命异常: {e}\n{traceback.format_exc()}")
            # [修正] 即使异常也建议返回 Success=True 防止卡死，或者 False 中断任务
            return CustomAction.RunResult(success=True)

# ==============================================================================
# 卡带名单：自选采集范围（WhiteListPatch）与调试直采（DebugListPatch）
# ==============================================================================
# 两者都从各自节点的 attach 读三类编号（StoryPack / CharacterPack / EventPack，界面输入框注入，
# 语法同屏蔽：1,3~5），只改具名卡带节点 Collect_Pack_<类>_<N> 的 enabled；节点范围从节点表枚举。
#   范围：全局生效，只做减法——范围外的具名卡带全部禁用，没填的类别整类关闭，无名卡兜底也关闭，不启用任何节点。
#         与排除（屏蔽）重叠时自然以排除为准，默认不支持采集的卡带（Story_20 / Character_8）也不会被打开。
#         三类都没有有效编号时视为没开，不修改。
#   调试：名单内的卡带启用（含默认不支持的），其余具名卡带与无名卡兜底全部禁用，没填的类整类不进。
#         名单内的卡带由独立的调试选卡分支放行，不受彩色徽章条件限制。
# 本周还有没有要采的，由下游 Collect_PackType_Select（action/pack_type_select.py）统一判断。
WHITE_LIST_NODE = "Collect_WhiteList_User"
DEBUG_LIST_NODE = "Collect_DebugList_User"
UNNAMED_PACK_NODE = "Collect_Pack_Unnamed"
PACK_TYPES = (
    ("StoryPack", "Collect_Pack_Story_"),
    ("CharacterPack", "Collect_Pack_Character_"),
    ("EventPack", "Collect_Pack_Event_"),
)


def _parse_param(raw):
    return raw if isinstance(raw, dict) else json.loads(str(raw) or "{}")


def _read_attach(context, node_name):
    try:
        return dict(getattr(context.get_node_object(node_name), "attach", None) or {})
    except Exception as e:
        mfaalog.warning(f"[卡带名单] 读取节点 {node_name} 的 attach 失败: {e}")
        return {}


def _pack_nodes(context, prefix):
    """节点表里的 <prefix><数字> -> {数字: 节点名}。"""
    try:
        names = [str(n) for n in context.tasker.resource.node_list]
    except Exception as e:
        mfaalog.error(f"[卡带名单] 读取节点表失败: {e}")
        return {}
    return {int(n[len(prefix):]): n for n in names
            if n.startswith(prefix) and n[len(prefix):].isdigit()}


def _is_enabled(context, node_name):
    try:
        data = context.get_node_data(node_name)
    except Exception:
        return True
    return not (isinstance(data, dict) and data.get("enabled") is False)


@AgentServer.custom_action("WhiteListPatch")
class WhiteListPatch(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = _parse_param(argv.custom_action_param)
        except Exception as e:
            mfaalog.error(f"[采集范围] 参数解析失败，本次不修改: {e}")
            return CustomAction.RunResult(success=True)
        attach = _read_attach(context, params.get("node_name", WHITE_LIST_NODE))

        keep = {}
        nodes_by_type = {}
        for key, prefix in PACK_TYPES:
            nums = parse_number_list(attach.get(key, ""))
            nodes = nodes_by_type[key] = _pack_nodes(context, prefix)
            keep[key] = [nodes[n] for n in nums if n in nodes]
            missing = [n for n in nums if n not in nodes]
            if missing:
                mfaalog.warning(f"[采集范围] {key} 的编号 {missing} 没有对应卡带，已忽略")
        if not any(keep.values()):
            mfaalog.warning("[采集范围] 已开启但没有有效编号，按未开启处理，不限制范围")
            return CustomAction.RunResult(success=True)

        patches = {}
        for key, nodes in nodes_by_type.items():
            for nm in nodes.values():
                if nm not in keep[key]:
                    patches[nm] = {"enabled": False}
            closed = [nm for nm in keep[key] if not _is_enabled(context, nm)]
            if closed:
                mfaalog.info(f"[采集范围] {key} 中 {closed} 已被排除或暂不支持采集，保持关闭")
            mfaalog.info(f"[采集范围] {key} 只保留 {keep[key] or '（无，整类不采）'}")
        # 没有具名节点的新卡也在范围外
        patches[UNNAMED_PACK_NODE] = {"enabled": False}

        try:
            context.override_pipeline(patches)
        except Exception as e:
            mfaalog.error(f"[采集范围] override_pipeline 失败: {e}")
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("DebugListPatch")
class DebugListPatch(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = _parse_param(argv.custom_action_param)
        except Exception as e:
            mfaalog.error(f"[卡带调试] 参数解析失败，本次不修改: {e}")
            return CustomAction.RunResult(success=True)
        attach = _read_attach(context, params.get("node_name", DEBUG_LIST_NODE))

        patches = {}
        picked = []
        for key, prefix in PACK_TYPES:
            nums = set(parse_number_list(attach.get(key, "")))
            nodes = _pack_nodes(context, prefix)
            missing = sorted(nums - set(nodes))
            if missing:
                mfaalog.warning(f"[卡带调试] {key} 的编号 {missing} 没有对应卡带，已忽略")
            for n, nm in nodes.items():
                patches[nm] = {"enabled": n in nums}
                if n in nums:
                    picked.append(nm)
        patches[UNNAMED_PACK_NODE] = {"enabled": False}

        if not picked:
            mfaalog.warning("[卡带调试] 调试直采已开启但名单为空，本次不会进入任何卡带")
        else:
            mfaalog.info(f"[卡带调试] 只进入 {picked}，其余卡带与无名卡兜底全部关闭")
        try:
            context.override_pipeline(patches)
        except Exception as e:
            mfaalog.error(f"[卡带调试] override_pipeline 失败: {e}")
        return CustomAction.RunResult(success=True)
