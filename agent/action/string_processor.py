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
# 强制采集（强采清单）应用器 —— 2026-10-02 本地新增
# ==============================================================================
# [用途] 界面选项「强制采集卡带」：只采名单里点名的卡带（语义B），并突破分类跳过闸。
# [与 BatchNumericPatch 的差别]
#   1. **按类判空**：某一类没填 -> 该类一个节点都不碰（既不禁用、也不动它的分类跳过闸），
#      完全保持上游原逻辑（本地记录显示该类已全完成 -> 照旧整类跳过）。
#      只有填了的那几类才做"清场 + 只启用名单 + 关掉该类的分类跳过闸"。
#   2. 清场范围从**节点表**枚举（Collect_Pack_<类>_<数字>），不写死 1~20/1~8/1~5，
#      上游增删卡带不用改这里。
#   3. 名单里若填了上游默认禁用的不可采卡带（Story_20 / Character_8），会被真的启用 —— 与
#      旧实现口径一致（界面说明里已写明）。
# [配套] 名单内的卡带连游戏内双勾也无视，放行逻辑在 recognition/pack_badge.py
#        （PackBadgeNotDone 里读同一个节点的 attach 做"强采放行"）。
# [闸名] 剧情/角色各有 Collect_QC_Skip_*；活动没有同类闸（只有 Event_Pending 交回闸）。
#        只要有任一类在强采，就必须关掉 Collect_QC_All_Done，否则"本地全都完成"会提前结束任务。
# ==============================================================================
FORCE_APPLY_NODE = "Collect_ForceList_Apply"
FORCE_CATS = (
    ("StoryPack", "Collect_Pack_Story_", "Collect_QC_Skip_Story"),
    ("CharacterPack", "Collect_Pack_Character_", "Collect_QC_Skip_Character"),
    ("EventPack", "Collect_Pack_Event_", None),
)
# 2026-10-09：各强采类对应的「类入口 Pending 节点」（剧情类没有，靠关 Collect_QC_Skip_Story
# 让调度器落到 [JumpBack]Collect_FeatureSwitch_StoryPack）。
FORCE_PENDING_BY_CAT = {
    "CharacterPack": "Collect_QC_Char_Pending",
    "EventPack": "Collect_QC_Event_Pending",
}


@AgentServer.custom_action("ForceListApply")
class ForceListApply(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            mfaalog.info("[ForceList] 引擎启动...")
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw) or "{}")
        except Exception as e:
            mfaalog.error(f"[ForceList] 参数解析失败: {e}")
            return CustomAction.RunResult(success=True)

        node_name = params.get("node_name", FORCE_APPLY_NODE)
        attach = {}
        try:
            obj = context.get_node_object(node_name)
            attach = dict(getattr(obj, "attach", None) or {})
        except Exception as e:
            mfaalog.warning(f"[ForceList] 读取节点 {node_name} 的 attach 失败: {e}")
        if not attach:
            mfaalog.warning(f"[ForceList] 节点 {node_name} 没有 attach（界面没填？）-> 本次不改任何节点")

        try:
            names = {str(n) for n in context.tasker.resource.node_list}
        except Exception:
            names = set()

        patches = {}
        active = []
        for key, pre, gate in FORCE_CATS:
            nums = parse_number_list(attach.get(key, ""))
            if not nums:
                mfaalog.info(f"[ForceList] {key} 未填写 -> 该类保持原逻辑（不禁用、不关闸）")
                continue
            # 先收集名单里真正存在的节点；一个都不存在就不能清场/禁用，否则会把
            # 整类全禁用却一张都不启用（Sourcery #4：填了数字但对不上节点）。
            on = []
            for n in nums:
                nm = f"{pre}{n}"
                if nm in names:
                    on.append(nm)
                else:
                    mfaalog.warning(f"[ForceList] {key} 填的 {n} 没有对应节点({nm})，忽略")
            if not on:
                mfaalog.warning(f"[ForceList] {key} 名单里的编号都对不上节点 -> 该类保持原逻辑")
                continue
            active.append(key)
            # 清场：同类具名节点全部禁用（按前缀+纯数字筛，从节点表枚举，不写死范围）
            for nm in names:
                if nm.startswith(pre) and nm[len(pre):].isdigit():
                    patches[nm] = {"enabled": False}
            # 只启用名单里的
            for nm in on:
                patches[nm] = {"enabled": True}
            if gate:
                patches[gate] = {"enabled": False}
            # 2026-10-09：强采还必须打通「类入口」的第二条路。
            # 分类跳过闸只管调度器那一条路；当剧情闸(Collect_QC_Skip_Story)命中时，
            # 路由会被带进闸链，角色/活动类的入口只剩 Collect_QC_*_Pending，
            # 而它的判据是存档 CheckCoolDown(match=any)——本周已打过标就判"没活"，
            # 与强采清单完全无关 ⇒ 出现"强采角色卡3却进不去角色分支"(实测 11:22)。
            # 故对填了强采的类，把对应 Pending 节点改成 DirectHit 恒命中，强制进入该类分支。
            # 安全：Pending 是被闸 next 以**普通跳转**引用的，只求值一次，不会像
            # JumpBack 目标那样被反复求值（那正是 11:54 死循环的成因）。
            pending = FORCE_PENDING_BY_CAT.get(key)
            if pending and pending in names:
                patches[pending] = {"recognition": "DirectHit"}
                mfaalog.info(f"[ForceList] {key} 强采 -> 类入口 {pending} 改 DirectHit（绕开存档判活）")
            mfaalog.info(
                f"[ForceList] {key} 强采: 只启用 {on}；同类其余全部禁用；"
                f"分类跳过闸 {gate or '(该类无此闸)'} 关闭"
                f"；类入口 {pending or '(该类无 Pending 节点，靠关闸进调度器路径)'}"
            )

        if active:
            patches["Collect_QC_All_Done"] = {"enabled": False}
            mfaalog.info(
                f"[ForceList] {len(active)} 类在强采 -> 关闭 Collect_QC_All_Done"
                f"（否则'全部完成'会提前结束任务）"
            )

        if patches:
            try:
                context.override_pipeline(patches)
                mfaalog.info(f"[ForceList] 执行完毕，注入 {len(patches)} 个节点补丁")
            except Exception as e:
                mfaalog.error(f"[ForceList] override_pipeline 失败: {e}")
        else:
            mfaalog.info("[ForceList] 三类都没填 -> 本次不改任何节点（行为与原版完全一致）")
        return CustomAction.RunResult(success=True)
