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
    Reset specified counters in the global TAG_STORE to zero.
    
    Parameters:
        params (dict): Action parameters; may include the key `"reset_tags"` whose value is either a string tag or a list of string tags. If `"reset_tags"` is absent or empty, the function does nothing.
    
    Behavior:
        For each tag provided, ensure TAG_STORE[tag] exists and is set to 0. Tags that were nonzero before the call are logged as having been reset.
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
            return False

        try:
            context.override_pipeline({target_node: backup_data})
            utils.mfaalog.info(f"[Py] 🔙 节点 [{target_node}] 已还原")
            
            # 还原后，从账本里移除？通常建议保留，方便反复修改。
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] RestoreNode 失败: {e}")
            return False

@AgentServer.custom_action("ResetAll")
class ResetAll(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
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
        """
        Apply a batch of pipeline patches and record original node data into the global shadow ledger when provided.
        
        Parameters:
            context (Context): Runtime context used to apply pipeline overrides via context.override_pipeline.
            argv (CustomAction.RunArg): Run argument containing JSON or a dict with:
                - "patches" (dict): required mapping of node names to patch data to apply.
                - "origins" (dict, optional): mapping of node names to original data to store in NODE_BACKUPS if not already present.
        
        Behavior:
            - Resets any requested tags via _process_reset_tags(params).
            - For each entry in "origins", if the node is not already present in the global NODE_BACKUPS, stores the provided origin data.
            - Calls context.override_pipeline with the "patches" dictionary to apply all modifications.
        
        Returns:
            bool: `True` if the patches were applied successfully, `False` if "patches" is missing or an error occurred while applying them.
        """
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
        """
        Apply regex-driven patches to cached node configurations and inject the resulting overrides into the pipeline.
        
        Processes rules (or a single rule) from the action parameters to match node names by regular expression and either set a deep value at `target_path` or apply a shallow `patch`. When a rule provides `origin`, that data is copied into the global shadow ledger `NODE_BACKUPS` for each matched node before modification. The special value `"$box"` is resolved to the current ROI derived from `argv.box`, and the placeholder `"$self"` inside values or patches is replaced with the matched node's name. If any overrides are produced, they are applied via `context.override_pipeline`. Also triggers tag resets via _process_reset_tags and ensures the node cache is loaded before matching.
        
        Parameters:
            context (Context): Execution context used to apply pipeline overrides.
            argv (CustomAction.RunArg): Action run argument; its `box` field (if provided) defines the current ROI, and its parsed JSON payload supplies either `rules` (list) or a single rule with keys:
                - `pattern` (str|list): regex or list of regexes to match node names.
                - `target_path` (list, optional): path (list of keys) for deep assignment; used with `value`.
                - `value` (any, optional): value to assign at `target_path`; `"$box"` resolves to the ROI.
                - `patch` (dict, optional): shallow patch object to apply; patch fields may contain `"$box"` or `"$self"`.
                - `origin` (any, optional): original node data to register in `NODE_BACKUPS` for matched nodes.
        
        Returns:
            bool: `True` if processing completed (and any produced overrides were applied), `False` on error or if the node cache is empty.
        """
        global NODE_BACKUPS 
        
        params = parse_json_arg(argv)
        _process_reset_tags(params)

        _ensure_cache_loaded()
        if not ALL_NODES_CACHE:
            return False

        current_roi = [0,0,0,0]
        if argv.box and getattr(argv.box, 'w', 0) > 0:
            current_roi = [int(argv.box.x), int(argv.box.y), int(argv.box.w), int(argv.box.h)]
        
        override_dict = {}
        matched_count = 0
        
        rules = params.get("rules")
        if rules is None:
            rules = [params]

        # ==========================================
        # 💡 [新增] 动态替换 $self 的辅助函数
        # 它可以钻进字典或数组的最深处，把 "$self" 换成节点真名
        # ==========================================
        def replace_self(data, current_node_name):
            """
            Recursively replaces all occurrences of the substring "$self" with the provided node name in strings found within nested structures.
            
            Parameters:
            	data: A value that may be a string, list, dict, or any nested combination thereof; strings inside this structure will have "$self" substituted.
            	current_node_name (str): The node name to substitute for each "$self" occurrence.
            
            Returns:
            	The input value with every string occurrence of "$self" replaced by `current_node_name`, preserving the original nested structure and non-string values unchanged.
            """
            if isinstance(data, str):
                return data.replace("$self", current_node_name)
            elif isinstance(data, list):
                return [replace_self(item, current_node_name) for item in data]
            elif isinstance(data, dict):
                return {k: replace_self(v, current_node_name) for k, v in data.items()}
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
                if final_deep_value == "$box": 
                    final_deep_value = current_roi
                
                for pat in patterns:
                    regex = re.compile(pat)
                    for node_name, original_config in ALL_NODES_CACHE.items():
                        base_config = override_dict.get(node_name, original_config)
                        
                        if regex.search(node_name):
                            # 影子账本登记
                            if origin_data and node_name not in NODE_BACKUPS:
                                NODE_BACKUPS[node_name] = copy.deepcopy(origin_data)
                            
                            # 执行修改
                            if target_path:
                                new_config = copy.deepcopy(base_config)
                                cursor = new_config
                                try:
                                    for key in target_path[:-1]:
                                        cursor = cursor[key]
                                        
                                    # [应用 $self]: 给深度路径赋的值进行检查替换
                                    actual_val = replace_self(final_deep_value, node_name)
                                    cursor[target_path[-1]] = actual_val
                                    
                                    override_dict[node_name] = new_config
                                    matched_count += 1
                                except Exception as e:
                                    # [改进] 抛出警告，吃掉报错的隐患已修复
                                    utils.mfaalog.warning(f"[Py] 深度路径修改失败 [{node_name}]: {e}")
                                    continue
                                    
                            elif simple_patch:
                                patch_copy = copy.deepcopy(simple_patch)
                                if patch_copy.get("roi") == "$box":
                                    patch_copy["roi"] = current_roi
                                
                                # [应用 $self]: 对整个补丁包进行检查替换
                                patch_copy = replace_self(patch_copy, node_name)
                                
                                # 保留你原本的覆盖逻辑，交由 MAA 底层处理合并
                                override_dict[node_name] = patch_copy
                                matched_count += 1

            if override_dict:
                context.override_pipeline(override_dict)
                utils.mfaalog.info(f"[Py] ⚡ [PatchRegex] 注入 {matched_count} 个节点的修改。")
            return True
        except Exception as e:
            utils.mfaalog.error(f"[Py] PatchByRegex 整体异常: {e}")
            return False