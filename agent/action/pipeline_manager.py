import json
import re
import os
import copy
import random
from pathlib import Path
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context
import utils
from recognition.counter import TAG_STORE

# ==============================================================================
# 🔧 Pipeline 动态管理器 
# ==============================================================================
# 实现了“影子账本”机制，支持自动记录原值、单点还原、批量重置。
# 同时集成了“旁作用(Side Effect)”机制，支持在执行动作时顺手重置计数器。
#
# ------------------------------------------------------------------------------
# 1. PatchNode (打补丁 + 自动注册备份 + 旁作用重置)
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "PatchNode",
# "custom_action_param": {
#     "node": "Battle_Node",                                  // [必填] 目标节点名
#     "patch": { "next": ["Boss_Win"], "timeout": 30000 },    // [必填] 要修改的参数字典
#     "origin": { "next": ["Normal_Win"], "timeout": 10000 }, // [选填] 原版数据备份。若缺略：首次运行时不会记录备份，导致无法还原。
#     "reset_tags": ["Battle_Count", "Win_Count"]             // [选填] 旁作用：顺手重置这些计数器。若缺略：不执行重置。
# }
# * 注意：如果你填了 'origin'，系统会自动把它存入内存。
# * 以后调用 RestoreNode 或 ResetAll 时，不需要再填数据，系统会自动查找。
# ------------------------------------------------------------------------------
# 2. PatchBatch (批量修改 + 自动注册备份 + 旁作用重置)
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "PatchBatch",
# "custom_action_param": {
#     "patches": {                                            // [必填] 补丁字典 {节点: 参数}
#         "Battle_Node": { "next": ["Boss_Win"], "timeout": 30000 },
#         "Swipe_Common": { "duration": 500 }
#     },
#     "origins": {                                            // [选填] 原版字典 {节点: 参数}。若缺略：不记录备份。
#         "Battle_Node": { "next": ["Normal_Win"], "timeout": 10000 },
#         "Swipe_Common": { "duration": 1000 }
#     },
#     "reset_tags": ["Daily_Loop_Count"]                      // [选填] 旁作用：顺手重置计数器。若缺略：不执行重置。
# }
# ------------------------------------------------------------------------------
# 3. PatchAndClick (魔改 + 偏移点击 + 旁作用重置)
# ------------------------------------------------------------------------------
# 场景：识别到入口 -> 1.修改后续节点参数 -> 2.点击当前识别位置(支持偏移)
#
# "action": "Custom",
# "custom_action": "PatchAndClick",
# "custom_action_param": {
#     "node": "Battle_Logic",                                 // [选填] 目标节点。若缺略：不执行Patch，仅执行点击。
#     "patch": { "next": ["Boss_Win"] },                      // [选填] 修改内容。若缺略：同上。
#     "origin": { "next": ["Common_Win"] },                   // [选填] 原版备份。
#     "target_offset": [100, 50, 0, 0],                       // [选填] 点击偏移量 [x, y, w, h]。X正向右，Y正向下。若缺略：点击识别框中心。
#     "reset_tags": ["Click_Count"]                           // [选填] 旁作用：顺手重置计数器。
# }
# ==============================================================================
# ------------------------------------------------------------------------------
# 4. RestoreNode (单点还原)
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "RestoreNode",
# "custom_action_param": {
#     "node": "Battle_Node"  <-- 只要名字，系统去账本里找原版数据
# }
#
# ------------------------------------------------------------------------------
# 5. ResetAll (一键重置/批量还原)
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "ResetAll"  <-- 不需要参数，把所有改过的节点都恢复
#
# ------------------------------------------------------------------------------
# 6. RunTask (调用子任务 + 旁作用重置)
# ------------------------------------------------------------------------------
# 作用: 运行另一个任务/节点，支持传入临时参数。注意，流程级别调用，参数修改节点跑完就清除了。
# "action": "Custom",
# "custom_action": "RunTask",
# "custom_action_param": {
#     "entry": "Swipe_Common_Node",           // [必填] 入口节点名
#     "param": {                              // [选填] 临时覆盖参数 (只在这次调用生效)。若缺略：使用原参数运行。
#         "Swipe_Common_Node": {              
#             "begin": [100, 200, 0, 0],
#             "duration": 500
#         }
#     },
#     "reset_tags": ["SubTask_Counter"]       // [选填] 旁作用：在启动子任务前重置计数器。若缺略：不重置。
# }
# ==============================================================================
# ==============================================================================
# 🔧 Pipeline 动态管理器 
# ==============================================================================
# 实现了“影子账本”机制，支持自动记录原值、单点还原、批量重置。
# 同时集成了“旁作用(Side Effect)”机制，支持在执行动作时顺手重置计数器。
#
# ------------------------------------------------------------------------------
# 1. PatchNode (打补丁 + 自动注册备份 + 旁作用重置)
# ------------------------------------------------------------------------------
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "PatchNode",
#         "custom_action_param": {
#             "node": "Battle_Node",                                  // [必填] 目标节点名
#             "patch": { "next": ["Boss_Win"], "timeout": 30000 },    // [必填] 要修改的参数字典
#             "origin": { "next": ["Normal_Win"], "timeout": 10000 }, // [选填] 原版数据备份。若缺略：首次运行时不会记录备份，导致无法还原。
#             "reset_tags": ["Battle_Count", "Win_Count"]             // [选填] 旁作用：顺手重置这些计数器。若缺略：不执行重置。
#         }
#     }
# }
# * 注意：如果你填了 'origin'，系统会自动把它存入内存。
# * 以后调用 RestoreNode 或 ResetAll 时，不需要再填数据，系统会自动查找。
# ------------------------------------------------------------------------------
# 2. PatchBatch (批量修改 + 自动注册备份 + 旁作用重置)
# ------------------------------------------------------------------------------
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "PatchBatch",
#         "custom_action_param": {
#             "patches": {                                            // [必填] 补丁字典 {节点: 参数}
#                 "Battle_Node": { "next": ["Boss_Win"], "timeout": 30000 },
#                 "Swipe_Common": { "duration": 500 }
#             },
#             "origins": {                                            // [选填] 原版字典 {节点: 参数}。若缺略：不记录备份。
#                 "Battle_Node": { "next": ["Normal_Win"], "timeout": 10000 },
#                 "Swipe_Common": { "duration": 1000 }
#             },
#             "reset_tags": ["Daily_Loop_Count"]                      // [选填] 旁作用：顺手重置计数器。若缺略：不执行重置。
#         }
#     }
# }
# ------------------------------------------------------------------------------
# 3. PatchAndClick (魔改 + 偏移点击 + 旁作用重置)
# ------------------------------------------------------------------------------
# 场景：识别到入口 -> 1.修改后续节点参数 -> 2.点击当前识别位置(支持偏移)
#
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "PatchAndClick",
#         "custom_action_param": {
#             "node": "Battle_Logic",                                 // [选填] 目标节点。若缺略：不执行Patch，仅执行点击。
#             "patch": { "next": ["Boss_Win"] },                      // [选填] 修改内容。若缺略：同上。
#             "origin": { "next": ["Common_Win"] },                   // [选填] 原版备份。
#             "target_offset": [100, 50, 0, 0],                       // [选填] 点击偏移量 [x, y, w, h]。X正向右，Y正向下。若缺略：点击识别框中心。
#             "reset_tags": ["Click_Count"]                           // [选填] 旁作用：顺手重置计数器。
#         }
#     }
# }
# ==============================================================================
# ------------------------------------------------------------------------------
# 4. RestoreNode (单点还原)
# ------------------------------------------------------------------------------
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "RestoreNode",
        # "custom_action_param": {
        #     "node": "Battle_Node"  <-- 只要名字，系统去账本里找原版数据
        # }
#     }
# }
#
# ------------------------------------------------------------------------------
# 4-2. RestoreBatch (复数还原)
# ------------------------------------------------------------------------------
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "RestoreBatch",
        # "custom_action_param": {
        #     "nodes": ["Node_A", "Node_B", "Node_C"],  <-- 只要名字，系统去账本里找原版数据
        #     "reset_tags": ["Tag_To_Reset"] // [可选] 顺手重置计数器
        # }
#     }
# }
# ------------------------------------------------------------------------------
# 5. ResetAll (一键重置/批量还原)
# ------------------------------------------------------------------------------
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "ResetAll"  <-- 不需要参数，把所有改过的节点都恢复
#     }
# }
#
# ------------------------------------------------------------------------------
# 6. RunTask (调用子任务 + 旁作用重置)
# ------------------------------------------------------------------------------
# 作用: 运行另一个任务/节点，支持传入临时参数。注意，流程级别调用，参数修改节点跑完就清除了。
# "action": {
#     "type": "Custom",
#     "param": {
#         "custom_action": "RunTask",
#         "custom_action_param": {
#             "entry": "Swipe_Common_Node",           // [必填] 入口节点名
#             "param": {                              // [选填] 临时覆盖参数 (只在这次调用生效)。若缺略：使用原参数运行。
#                 "Swipe_Common_Node": {              
#                     "begin": [100, 200, 0, 0],
#                     "duration": 500
#                 }
#             },
#             "reset_tags": ["SubTask_Counter"]       // [选填] 旁作用：在启动子任务前重置计数器。若缺略：不重置。
#         }
#     }
# }
# ==============================================================================
# 7. PatchByRegex (正则批量覆写 - 增强账本支持版)
# ------------------------------------------------------------------------------
# 场景：通过正则表达式一次性修改大批节点。完全支持影子账本和多规则并发。
#
# "action": "Custom",
# "custom_action": "PatchByRegex",
# "custom_action_param": {
#     "reset_tags": ["TagA"],                 // [选填] 旁作用：顺手重置计数器
#     "rules": [                              // 规则数组（如果是单套规则，可省略 rules 直接写在外层）
#         {
#             "pattern": ".*_Swip_.*",                // [必填] 要匹配的节点正则
#             "patch": { "timeout": 5000 },           // [选填] 修改方案 1: 浅层合并覆盖
#             "origin": { "timeout": 10000 }          // [关键] 登记账本：为所有匹配到的节点统一指定此还原数据
#         },
#         {
#             "pattern": "^Collect_Pack_.*",
#             "target_path": ["all_of", 0, "roi"],    // [选填] 修改方案 2: 深度路径替换(专用于替换数组内字段,先全部获取,由py合并后再全量覆盖)
#             "value": "$box",
#             "origin": {                             // 对于深度修改，origin 依然需要填写合并用字典格式
#                 "all_of": [{"roi": [0,0,0,0]}] 
#             }
#         },
#         {
#             "pattern": "^Collect_LocatePackFrame_Loc[2-9]$", 
#             "patch": {                              // [选填] 修改方案 3: 动态自我引用 ($self)
#                 "next": [
#                     "Collect_Loc_OutToSwip_Hub_Ingress",
#                     "[JumpBack]$self"               // $self 会在运行时被自动替换为实际匹配到的节点真名
#                 ]                                   // (例如匹配到 Loc9 时，这里会变成 [JumpBack]Collect_LocatePackFrame_Loc9)
#             }
#     ]
# }
# ------------------------------------------------------------------------------
# {
#     "💡模板_单套正则修改_附带账本记录": {
#         "action": "Custom",
#         "custom_action": "PatchByRegex",
#         "custom_action_param": {
#             "reset_tags": [
#                 "PackLocation_ToggleSwitch"
#             ],
#             "pattern": [
#                 "^Collect_LocatePackFrame_Smart_Swip$"
#             ],
#             "target_path": [
#                 "custom_recognition_param",
#                 "max"
#             ],
#             "value": 3,
#             "origin": {
#                 "custom_recognition_param": {
#                     "max": 0,
#                     "tag": "LocPage_SmartSwip"
#                 }
#             }
#         },
#         "doc": "用法：把正则匹配到的节点，按照 target_path 修改。同时把 origin 字典塞入账本。下次无论用单点 RestoreNode 还是 ResetAll，系统都会取这个 origin 来恢复它。"
#     },
#     "💡模板_多套规则_高级独立账本操作": {
#         "action": "Custom",
#         "custom_action": "PatchByRegex",
#         "custom_action_param": {
#             "rules": [
#                 {
#                     "pattern": [
#                         "^Collect_Pack_(Story|Character|Event)_\\d+$"
#                     ],
#                     "target_path": [
#                         "all_of",
#                         0,
#                         "roi"
#                     ],
#                     "value": "$box",
#                     "origin": {
#                         "all_of": [
#                             {
#                                 "roi": [1183, 603, 51, 56]
#                             }
#                         ]
#                     }
#                 },
#                 {
#                     "pattern": [
#                         "^Shop_Buy_Item_.*$"
#                     ],
#                     "patch": {
#                         "timeout": 5000,
#                         "next": ["Shop_Out"]
#                     },
#                     "origin": {
#                         "timeout": 15000,
#                         "next": ["Shop_Continue"]
#                     }
#                 }
#             ]
#         },
#         "doc": "用法：每个 rule 是独立的宇宙。A 类节点登记 A 类的 origin，B 类节点登记 B 类的 origin，互不干涉。"
#     },
#     "💡模板_单点还原_精准恢复某个特定节点": {
#         "action": "Custom",
#         "custom_action": "RestoreNode",
#         "custom_action_param": {
#             "node": "Shop_Buy_Item_3"
#         },
#         "next": [
#             "Next_Pipeline_Node"
#         ],
#         "doc": "高阶运用：上面的正则虽然改了所有的 Shop_Buy_Item，但我现在只觉得 3 号买完了，我单独对 3 号调用 RestoreNode，把它从影子账本里单独拉出来恢复。"
#     }
# }
# ==============================================================================
# --- 全局影子账本 ---
# 格式: { "NodeName": { "original_key": "original_value" } }
NODE_BACKUPS = {}          # 影子账本：记录被修改节点的原值
ALL_NODES_CACHE = {} 
CACHE_LOADED = False

def parse_json_arg(argv: CustomAction.RunArg) -> dict:
    """通用参数解析器"""
    try:
        if not argv.custom_action_param: return {}
        if isinstance(argv.custom_action_param, dict): return argv.custom_action_param
        return json.loads(str(argv.custom_action_param).strip())
    except: return {}

def _ensure_cache_loaded(force_refresh=False):
    """建立本地节点配置数据库 (Deep Cache)"""
    global ALL_NODES_CACHE, CACHE_LOADED
    if CACHE_LOADED and not force_refresh: return

    utils.mfaalog.info("[Py] 💾 正在建立节点数据库 (Deep Cache)...")
    ALL_NODES_CACHE = {} 
    base_dir = Path(".") 
    target_path = base_dir / "resource" / "pipeline"
    if not target_path.exists():
        found = list(base_dir.rglob("pipeline"))
        if found: target_path = found[0]

    if not target_path.exists():
        utils.mfaalog.error(f"[Py] ❌ 找不到 pipeline 目录")
        return

    count = 0
    for file_path in target_path.rglob("*.json"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content_str = f.read()
                content_str = re.sub(r"//.*", "", content_str)
                data = json.loads(content_str)
            if isinstance(data, dict):
                for node_name, node_config in data.items():
                    ALL_NODES_CACHE[node_name] = node_config
                    count += 1
        except Exception as e:
            utils.mfaalog.warning(f"[Py] 读取跳过 {file_path.name}: {e}")

    CACHE_LOADED = True
    utils.mfaalog.info(f"[Py] 💾 数据库构建完成！已索引 {count} 个节点的原始配置。")

def _process_reset_tags(params: dict):
    """
    [新增] 通用旁作用：处理标签重置
    在任何 Action 里调用这个函数，就能顺手把计数器清了
    """
    global TAG_STORE
    raw_tags = params.get("reset_tags") # 统一参数名: reset_tags
    
    if not raw_tags:
        return

    target_list = raw_tags if isinstance(raw_tags, list) else [raw_tags]
    reset_logs = []
    
    for tag in target_list:
        # 只要 TAG_STORE 里有这个键，或者你想强制初始化为0，都可以
        # 这里为了安全，只重置已存在的
        if tag in TAG_STORE or True: # 这里的 True 表示允许初始化新tag
            if TAG_STORE.get(tag, 0) != 0:
                TAG_STORE[tag] = 0
                reset_logs.append(tag)
            else:
                # 已经是0了，确保存在即可
                TAG_STORE[tag] = 0
                
    if reset_logs:
        utils.mfaalog.info(f"[Py] 🧹 [旁作用] 顺手清零了标签: {reset_logs}")

@AgentServer.custom_action("PatchNode")
class PatchNode(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global NODE_BACKUPS
        params = parse_json_arg(argv)
        
        # 1. [旁作用] 处理计数器重置
        _process_reset_tags(params)

        # 2. 主逻辑
        target_node = params.get("node")
        patch_data = params.get("patch")
        origin_data = params.get("origin") # 用户手动提供的原版数据

        if not target_node or not patch_data:
            utils.mfaalog.error("[Py] PatchNode 缺参数 (node/patch)")
            return False

        try:
            # 如果用户提供了原版数据，且账本里还没有记录，就记下来
            if origin_data and target_node not in NODE_BACKUPS:
                NODE_BACKUPS[target_node] = origin_data
                utils.mfaalog.info(f"[Py] 📖 已登记节点备份: {target_node}")

            # 执行魔改
            context.override_pipeline({target_node: patch_data})
            utils.mfaalog.info(f"[Py] 🔧 节点 [{target_node}] 已打补丁")
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] PatchNode 失败: {e}")
            return False

@AgentServer.custom_action("RestoreNode")
class RestoreNode(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """
        将指定节点的配置还原为先前保存的备份或调用时提供的备份数据。
        
        尝试按优先级从内部备份账本中读取目标节点的备份；如果不存在，则使用参数中 `backup` 字段（兼容旧写法）。如果既没有备份也未提供 `backup`，记录警告并作为无操作返回成功状态；如果恢复过程中发生异常则返回失败。
        Returns:
            True 如果成功应用恢复或在无备份时作为无操作完成，False 如果缺少必需的 `node` 参数或恢复过程中发生错误。
        """
        global NODE_BACKUPS
        params = parse_json_arg(argv)
        target_node = params.get("node")

        if not target_node:
            return False

        # 1. 优先从账本里找
        backup_data = NODE_BACKUPS.get(target_node)
        
        # 2. 如果账本里没有，看看用户有没有临时传 backup 参数 (兼容旧写法)
        if not backup_data:
            backup_data = params.get("backup")

        if not backup_data:
            utils.mfaalog.warning(f"[Py] ⚠️ 无法还原 [{target_node}]：未在Patch时登记origin，也未传入backup参数")
            return True

        try:
            context.override_pipeline({target_node: backup_data})
            utils.mfaalog.info(f"[Py] 🔙 节点 [{target_node}] 已还原")
            
            # 还原后，从账本里移除？通常建议保留，方便反复修改。
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] RestoreNode 失败: {e}")
            return False

@AgentServer.custom_action("RestoreBatch")
class RestoreBatch(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """
        根据备份账本（NODE_BACKUPS）将参数中指定的节点批量还原到管线上下文。
        
        函数会处理 params 中的重置标签（reset_tags）作为一项副作用。params 需包含 "nodes" 字段且为节点名列表；函数将从全局备份中挑选存在的节点并通过 context.override_pipeline 应用还原。若列表中有未找到的备份会记录警告并忽略之；当没有有效还原项时视为无操作并返回成功。
        
        Parameters:
            argv (CustomAction.RunArg): 运行时参数，解析后应包含 "nodes" 键，对应要还原的节点名列表。
        
        Returns:
            bool: `True` 如果还原已完成或没有需要还原的节点（包括内部异常被抑制的情况），`False` 如果输入参数无效（例如 "nodes" 缺失或不是列表）。
        """
        global NODE_BACKUPS
        params = parse_json_arg(argv)
        
        # 1. [旁作用] 处理计数器重置
        _process_reset_tags(params)

        # 2. 获取目标节点列表
        target_nodes = params.get("nodes")
        
        if not target_nodes or not isinstance(target_nodes, list):
            utils.mfaalog.warning("[Py] RestoreBatch 参数错误: 'nodes' 必须是列表")
            return False

        # 3. 构建还原字典
        restore_dict = {}
        missing_nodes = []

        for node in target_nodes:
            if node in NODE_BACKUPS:
                restore_dict[node] = NODE_BACKUPS[node]
            else:
                missing_nodes.append(node)

        # 记录一下没找到的（可能是还没被Patch过，或者拼写错误）
        if missing_nodes:
            utils.mfaalog.warning(f"[Py] ⚠️ RestoreBatch: 账本中未找到以下节点的备份 (忽略): {missing_nodes}")

        if not restore_dict:
            utils.mfaalog.warning("[Py] ⚠️ RestoreBatch: 没有可还原的有效节点")
            return True

        try:
            # 4. 执行批量还原
            context.override_pipeline(restore_dict)
            utils.mfaalog.info(f"[Py] 🔙 批量还原成功: {list(restore_dict.keys())}")
            return True
        except Exception as e:
            # 发生未知代码异常？打印堆栈，然后返回 ....# 有待讨论，如果复位失败可能意外中断整个节点链
            utils.mfaalog.error(f"[Py] 💥 RestoreBatch 发生严重异常 (已抑制): {e}")
            import traceback
            utils.mfaalog.error(traceback.format_exc())
            return False

@AgentServer.custom_action("ResetAll")
class ResetAll(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """
        将所有已保存的节点备份批量还原到管线并清空备份账本。
        
        在有备份时调用管线的 override_pipeline 来恢复所有节点配置，随后清空 NODE_BACKUPS 以移除已应用的备份记录；如果没有备份则立即返回成功。
        
        Returns:
            True 如果所有备份已成功还原或没有需要重置的节点，False 如果在还原过程中发生错误。
        """
        global NODE_BACKUPS
        
        if not NODE_BACKUPS:
            utils.mfaalog.info("[Py] 🧹 没有需要重置的节点")
            return True
            
        try:
            utils.mfaalog.info(f"[Py] 🧹 正在批量重置 {len(NODE_BACKUPS)} 个节点...")
            
            # 批量还原
            context.override_pipeline(NODE_BACKUPS)
            
            utils.mfaalog.info("[Py] ✅ 所有热更改已清除，节点已复原")
            
            # 清空账本
            NODE_BACKUPS.clear()
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] ResetAll 异常: {e}")
            return False

@AgentServer.custom_action("RunTask")
class RunTask(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        params = parse_json_arg(argv)
        
        # 1. [旁作用] 处理计数器重置
        _process_reset_tags(params)

        # 2. 主逻辑
        entry_node = params.get("entry")
        # 新增：读取 override 参数
        override_data = params.get("param", {}) 
        
        if not entry_node:
            return False

        try:
            if override_data:
                utils.mfaalog.info(f"[Py] 🚀 Call Sub: [{entry_node}] (带参数注入)")
                # 第二个参数就是 运行时覆盖，它只在这次运行中生效，不污染全局
                context.run_task(entry_node, override_data)
            else:
                utils.mfaalog.info(f"[Py] 🚀 Call Sub: [{entry_node}]")
                context.run_task(entry_node)
                
            utils.mfaalog.info(f"[Py] ✅ Return: 子任务结束")
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] RunTask 异常: {e}")
            return False
        
@AgentServer.custom_action("PatchAndClick")
class PatchAndClick(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global NODE_BACKUPS
        
        try:
            params = parse_json_arg(argv)
            
            # 1. [旁作用] 处理计数器重置
            _process_reset_tags(params)
            
            # --- 步骤 1: 解析参数 & 执行 Patch 逻辑 ---
            target_node = params.get("node")
            patch_data = params.get("patch")
            origin_data = params.get("origin")
            
            # [读取] 偏移量
            user_offset = params.get("target_offset") 

            if target_node and patch_data:
                # 1.1 登记备份
                if origin_data and target_node not in NODE_BACKUPS:
                    NODE_BACKUPS[target_node] = origin_data
                    utils.mfaalog.info(f"[Py] 📖 (P&C) 已登记节点备份: {target_node}")

                # 1.2 执行魔改
                context.override_pipeline({target_node: patch_data})
                print(f"[Py] 🔧 (P&C) 节点 [{target_node}] 参数已动态替换")
            else:
                print("[Py] PatchAndClick 缺少 patch 参数，仅执行点击逻辑")

            # --- 步骤 2: 执行点击逻辑 (Click) ---
            box = argv.box
            
            if box:
                # 兼容属性获取
                w = getattr(box, 'w', getattr(box, 'width', 0))
                h = getattr(box, 'h', getattr(box, 'height', 0))
                
                # 只有当识别到了东西，或者用户想盲点（虽然通常必须有box）
                if w > 0 and h > 0:
                    # 默认点击识别区域的中心
                    final_x = box.x + w / 2
                    final_y = box.y + h / 2
                    
                    # [核心逻辑] 处理自定义偏移
                    if user_offset:
                        # 🔒 严格模式：必须是 4 位数组 [dx, dy, w, h]
                        if not isinstance(user_offset, list) or len(user_offset) != 4:
                            print(f"[Py] ❌ 参数错误: target_offset 必须包含 4 个值 [dx, dy, w, h]。当前: {user_offset}")
                            return False
                        
                        off_dx = int(user_offset[0])
                        off_dy = int(user_offset[1])
                        off_w  = int(user_offset[2])
                        off_h  = int(user_offset[3])
                        
                        # 计算新区域的左上角
                        target_x = box.x + off_dx
                        target_y = box.y + off_dy
                        
                        # 计算新区域的中心点 (如果是 0,0 则就是左上角本身)
                        final_x = target_x + off_w / 2
                        final_y = target_y + off_h / 2
                        
                        utils.mfaalog.info(f"[Py] 🎯 应用精确偏移: 基准({box.x},{box.y}) -> 偏移[{off_dx},{off_dy},{off_w},{off_h}]")

                    # 移除随机微调，相信作者的设定
                    click_x = int(final_x)
                    click_y = int(final_y)
                    
                    # 2.4 执行点击
                    context.tasker.controller.post_click(click_x, click_y)
                    utils.mfaalog.info(f"[Py] 🖱️ (P&C) 点击坐标: ({click_x}, {click_y})")
                    return True
            
            print("[Py] ⚠️ Patch 成功，但没有有效的识别区域(Box)，无法点击。")
            return True

        except Exception as e:
            utils.mfaalog.error(f"[Py] PatchAndClick 异常: {e}")
            return False
        
@AgentServer.custom_action("PatchBatch")
class PatchBatch(CustomAction):
    """
    V2: 批量打补丁
    支持格式:
    {
        "patches": { 
            "NodeA": { "param": "val" },
            "NodeB": { "param": "val" }
        },
        "origins": {
            "NodeA": { "param": "old_val" },
            "NodeB": { "param": "old_val" }
        },
        "reset_tags": ["TagA"]
    }
    """
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global NODE_BACKUPS
        params = parse_json_arg(argv)
        
        # 1. [旁作用] 处理计数器重置
        _process_reset_tags(params)

        # 2. 主逻辑
        # 获取整个补丁字典 { "NodeName": {data}, ... }
        patches_dict = params.get("patches", {})
        origins_dict = params.get("origins", {})

        if not patches_dict:
            utils.mfaalog.warning("[Py] PatchBatch: 未提供 patches 数据")
            return False

        try:
            # 批量登记备份 (仅当账本中不存在时)
            for node_name, origin_data in origins_dict.items():
                if node_name not in NODE_BACKUPS:
                    NODE_BACKUPS[node_name] = origin_data
                    utils.mfaalog.info(f"[Py] 📖 (Batch) 已登记备份: {node_name}")

            # 批量执行魔改
            context.override_pipeline(patches_dict)
            
            utils.mfaalog.info(f"[Py] 🔧 (Batch) 已同时修改 {len(patches_dict)} 个节点")
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] PatchBatch 失败: {e}")
            return False
        
# PatchByRegex (正则批量覆写)
# ==============================================================================
@AgentServer.custom_action("PatchByRegex")
class PatchByRegex(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        global NODE_BACKUPS 
        
        params = parse_json_arg(argv)
        _process_reset_tags(params)

        _ensure_cache_loaded()
        if not ALL_NODES_CACHE:
            return False

        # 获取当前识别到的框
        current_roi = [0,0,0,0]
        if argv.box and getattr(argv.box, 'w', 0) > 0:
            current_roi = [int(argv.box.x), int(argv.box.y), int(argv.box.w), int(argv.box.h)]
        
        override_dict = {}
        matched_count = 0
        
        # 兼容性处理：如果没有 rules 数组，把外层参数当作单条规则处理
        rules = params.get("rules")
        if rules is None:
            rules = [params]

        # ==========================================
        # 💡 [新增] 动态替换 $self 的辅助函数
        # 它可以钻进字典或数组的最深处，把 "$self" 换成节点真名
        # ==========================================

        # --- 内部辅助函数：深度合并字典 (防止 simple_patch 抹除原有属性) ---
        def deep_merge(tgt, src):
            for k, v in src.items():
                if isinstance(v, dict) and k in tgt and isinstance(tgt[k], dict):
                    deep_merge(tgt[k], v)
                else:
                    tgt[k] = copy.deepcopy(v)
            return tgt

        # --- 内部辅助函数：动态替换 $self 占位符 ---
        def replace_self(data, node_name):
            if isinstance(data, str):
                return data.replace("$self", node_name)
            elif isinstance(data, list):
                return [replace_self(item, node_name) for item in data]
            elif isinstance(data, dict):
                return {k: replace_self(v, node_name) for k, v in data.items()}
            return data
        
        try:
            for rule in rules:
                patterns = rule.get("pattern")
                if not patterns: 
                    continue
                if isinstance(patterns, str): 
                    patterns = [patterns]
                
                target_path = rule.get("target_path") 
                deep_value = rule.get("value") 
                simple_patch = rule.get("patch") 
                origin_data = rule.get("origin") 
                
                final_deep_value = deep_value
                
                for pat in patterns:
                    regex = re.compile(pat)
                    for node_name, original_config in ALL_NODES_CACHE.items():
                        # 获取当前节点的最新状态（可能是原始配置，也可能是被前一条规则修改过的配置）
                        base_config = override_dict.get(node_name, original_config)
                        
                        if regex.search(node_name):
                            # ------------------------------------------------------
                            # [逻辑警告] 影子账本登记：先到先得原则
                            # ------------------------------------------------------
                            # 只有当该节点尚未在账本中时，才记录 origin。
                            # 这意味着如果多个规则都试图设定 origin，只有第一条生效。
                            # 设计意图：防止多次修改导致“原始还原点”被后续规则污染。
                            # ------------------------------------------------------
                            if origin_data and node_name not in NODE_BACKUPS:
                                NODE_BACKUPS[node_name] = copy.deepcopy(origin_data)
                            
                            # === 模式 1: 深度路径替换 ===
                            if target_path:
                                new_config = copy.deepcopy(base_config)
                                cursor = new_config
                                try:
                                    for key in target_path[:-1]:
                                        cursor = cursor[key]
                                    
                                    # 处理 $box
                                    actual_val = final_deep_value
                                    if actual_val == "$box":
                                        # 无效 ROI 直接跳过本次修改，防止注入 [0,0,0,0]
                                        if not current_roi or current_roi == [0,0,0,0]:
                                            utils.mfaalog.warning(f"[Py] ⚠️ [PatchRegex] 规则试图注入 $box 但无有效 ROI，跳过此节点: {node_name}")
                                            continue 
                                        actual_val = current_roi
                                    
                                    # 处理 $self
                                    actual_val = replace_self(actual_val, node_name)
                                    
                                    cursor[target_path[-1]] = actual_val
                                    override_dict[node_name] = new_config
                                    matched_count += 1
                                except Exception as e:
                                    utils.mfaalog.warning(f"[Py] 深度路径修改失败 [{node_name}]: {e}")
                                    continue
                                    
                            # === 模式 2: 浅层补丁合并 ===
                            elif simple_patch:
                                new_config = copy.deepcopy(base_config)
                                patch_copy = copy.deepcopy(simple_patch)
                                
                                # 处理 $box
                                if patch_copy.get("roi") == "$box":
                                    # [修正] 无效 ROI 只删字段，保留 timeout/next 等其他修改
                                    if not current_roi or current_roi == [0,0,0,0]:
                                        utils.mfaalog.warning(f"[Py] ⚠️ [PatchRegex] 规则试图注入 $box 但无有效 ROI，已移除该 ROI 字段，其他参数继续生效: {node_name}")
                                        del patch_copy["roi"]
                                    else:
                                        patch_copy["roi"] = current_roi
                                
                                # 处理 $self
                                patch_copy = replace_self(patch_copy, node_name)
                                
                                # 使用 deep_merge 安全合并，防止抹除原节点其他属性
                                deep_merge(new_config, patch_copy)
                                
                                override_dict[node_name] = new_config
                                matched_count += 1

            if override_dict:
                context.override_pipeline(override_dict)
                utils.mfaalog.info(f"[Py] ⚡ [PatchRegex] 注入 {matched_count} 个节点的修改。")
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] PatchByRegex 整体异常: {e}")
            return False