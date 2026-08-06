import json
import os
import time
from datetime import datetime, timedelta, timezone
import utils

# ✅ 引入存档工具
# (不需要改 persistent_store.py，它负责底层读写，这里负责业务逻辑)
from utils.persistent_store import PersistentStore

# --- MFA 核心库 ---
from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.context import Context
from maa.agent.agent_server import AgentServer

utils.mfaalog.info(f"[Py] 周期策略管理器已加载。")

# ==============================================================================
# 🎮 周期策略管理器 (Cooldown & Cycle Manager)
# ==============================================================================
# 核心功能：基于 [游戏服务器时间] 和 [本地存储] 判断任务是否需要运行。
# 适用场景：日替副本、周常 Boss、半月深渊、限时活动等。
# 特性：支持全球时区设定。开发侧：类设定按照当地时间时区填入，后台自动同步为UTC+0。
#  。                   用户侧：时间戳后台自动同步为UTC+0，不妨碍计算。
# 策略漏洞：忽略了时区变化,时间戳没有时区标记，带电脑旅游历史时间戳的处理未编写应对。
#
# ------------------------------------------------------------------------------
# 📝 JSON Pipeline 配置指南 (标准规范版)
# ------------------------------------------------------------------------------
#
# 【模式 A】作为“自定义识别器”使用 (推荐 ⭐)
# ---------------------------------------------------
# 逻辑：检查通过 -> 视为“识别成功”，执行当前节点的 action。
#       检查不通过(冷却中) -> 视为“识别失败”，寻找 on_error 或跳过当前任务。
#
# "Task_Daily_Dungeon": {
#     "recognition": "Custom",               // ⚡️ 必须固定为 Custom
#     "custom_recognition": "CheckCoolDown", // ⚡️ 指向 Python 注册的 ID
#     "custom_recognition_param": {
#         "card_name": "Map_01",             // 任务唯一标识 ID
#         "cycle_type": "g_daily"            // 策略类型
#     },
#     "timeout": 100,                        // 建议设短（如 100ms），纯逻辑无需重试
#     "action": "Click",
#     "action_param": { "target": "Button" },
#     "next": [ "Sub_Task" ],
#     "on_error": [ "Next_Major_Task" ]      // 冷却中会触发失败，跳转至此
# }
#
# 【模式 B】作为“自定义动作”使用
# ---------------------------------------------------
# 逻辑：检查通过 -> Action 返回 True，继续 next。
#       检查不通过 -> Action 返回 False，通常导致节点失败。
#
# "Task_Check_Action": {
#     "action": "Custom",                    // ⚡️ 注意：Action 模式也建议写全
#     "custom_action": "CheckCoolDown",
#     "custom_action_param": {
#         "card_name": "Map_01"
#     },
#     "next": [ "Enter_Stage" ]
# }
#
# 【通用】任务完成标记 (MarkComplete)
# ---------------------------------------------------
# 单个:
# "Task_Combat_Done": {
#     "action": "Custom",
#     "custom_action": "MarkComplete",
#     "custom_action_param": {
#         "card_name": "Map_01",
#         "cycle_type": "g_daily" ←缺略默认
#     }
# }
#
# 多个:
# "custom_action_param": {
#     "targets": [          ←注意!
#         { "card_name": "Map_01", "cycle_type": "g_daily" },
#         { "card_name": "Map_Boss_Reward", "cycle_type": "g_weekly" },
#         { "card_name": "Map_Hidden_Path", "cycle_type": "g_weekly" }
#     ]
# }
# ------------------------------------------------------------------------------
# ⚙️ 策略配置说明 (CYCLE_STRATEGIES)
# ------------------------------------------------------------------------------
# type             : 周期模式 ("daily" | "weekly" | "semi_monthly" | "interval")
# reset_time       : 刷新时间点 (24小时制字符串，如 "04:00")
# timezone         : 服务器时区 (8=北京时间, 0=UTC, 9=东京时间)
# reset_weekday    : [weekly专用] 刷新日 (0=周一, ... 6=周日)
# reset_days       : [semi_monthly专用] 刷新日期列表 (如 [1, 16])
# anchor_date      : [interval专用] 历史上任意一次刷新日期 ("2024-01-01")
# interval_days    : [interval专用] 间隔天数 (14=双周, 3=每三天)
# blackout_minutes : 结算保护期 (分钟)。在此期间脚本将强制跳过任务。
#
# ==============================================================================
CYCLE_STRATEGIES = {
    # 【实例1】国服/日服手游通用 (当地服务器时间 凌晨4点刷新)
    # "cn_daily": {
    #     "type": "daily",
    #     "reset_time": "04:00",  # 凌晨4点刷新
    #     "timezone": 8,          # UTC+8 北京时间
    #     "blackout_minutes": 0   # 无结算期
    # },
    
    # # 【实例2】国际服通用 (UTC 0点刷新)
    # # 比如: 1999国际服, NIKKE等
    # "global_daily": {
    #     "type": "daily",
    #     "reset_time": "00:00",  # UTC 0点
    #     "timezone": 0,          # UTC+0
    #     "blackout_minutes": 0
    # },

    # # 【实例3】国服周常 (每周一 04:00)
    # "cn_weekly": {
    #     "type": "weekly",
    #     "reset_time": "04:00",
    #     "timezone": 8,
    #     "reset_weekday": 0,     # 周一
    #     "blackout_minutes": 10  # 结算10分钟，防止刚好卡点进不去
    # },

    # # 【实例4】深渊/爬塔 (半月常, 1号/16号刷新)
    # "cn_abyss": {
    #     "type": "semi_monthly",
    #     "reset_time": "04:00",
    #     "timezone": 8,
    #     "reset_days": [1, 16],  # 1号和16号
    #     "blackout_minutes": 60  # 结算1小时 (04:00-05:00不可进入)
    # }

    # # 【实例5】双周/间隔模式 (每14天刷新)
    # "biweekly_event": {
    #     "type": "interval",
    #     "interval_days": 14,          # 14天一循环
    #     "anchor_date": "2024-01-01",  # 锚点：历史上的一天刷新日
    #     "reset_time": "04:00",
    #     "timezone": 8,
    #     "blackout_minutes": 0
    # },
    #
    # #####第一个有效字典数组会被视为默认值!#####
    # 【BD2】国际服-卡带刷新时间周常 (每周一 08:00) 
    "g_weekly": {
        "type": "weekly",
        "reset_time": "08:00",
        "timezone": 8,
        "reset_weekday": 0,     # 周一
        "blackout_minutes": 0   # 无结算期
    },
    # 【BD2】国际服-日常刷新时间 (每天 08:00)
    "g_daily": {
        "type": "daily",
        "reset_time": "08:00",  # UTC 0点
        "timezone": 8,          # UTC+8
        "blackout_minutes": 0
    },
    # 【BD2】国际服-镜中之战刷新时间 (每14天刷新)
    "mirror_pvp": {
        "type": "interval",
        "interval_days": 7,          # 7天一循环
        "anchor_date": "2026-01-25",  # 锚点：历史上的一天刷新日
        "reset_time": "23:00",
        "timezone": 8,
        "blackout_minutes": 540      # 暂时设定。假定周日晚23点结束，有待确认:假定周一8点开始
    },
    # 【BD2】国际服-黄金竞技场刷新时间 (每14天刷新)
    # 原写法 "reset_time": "24:00" + reset_weekday=2(周三),意为"周三 24:00"即周四 0 点。
    # 但 weekly 的回溯是按 base_reset.weekday() 算的,而 base_reset 会先被回退一天,
    # 于是 24:00+周三 实际落到"周三 0 点",比意图早 24 小时 —— 这个歧义源自
    # "时刻跨到次日、weekday 却还写着前一天",代码层面消不掉,只能在配置里写明确。
    # 故等价改写为 "00:00" + 周四,刷新点仍是周四 0 点,语义不变且无歧义。
    "golden_pvp": {
        "type": "weekly",
        "reset_time": "00:00",
        "timezone": 8,
        "reset_weekday": 3,     # 周四 0 点(即原意的"周三 24:00")
        "blackout_minutes": 540  # 结算540分钟，防止刚好卡点进不去
    }
    # # 【BD2】国际服-救赎之塔 (半月常, 1号/16号刷新)时间不确定，暂时不写
    # "g_abyss": {
    #     "type": "semi_monthly",
    #     "reset_time": "04:00",
    #     "timezone": 8,
    #     "reset_days": [1, 16],  # 1号和16号
    #     "blackout_minutes": 60  # 结算1小时 (04:00-05:00不可进入)
    # }
}
# ==============================================================================


class CooldownManager:
    # ✅ 既然 PersistentStore 是静态类，这里甚至不需要 __init__
    def __init__(self):
        #性能优化：将本地时区在类初始化时缓存下来，避免每次重复调用系统 API
        self._local_tz = datetime.now().astimezone().tzinfo

    def _get_storage_key(self, card_name, strategy_name):
        """生成唯一存储键名 (防止不同策略共用同一个名字导致冲突)"""
        return f"{card_name}@{strategy_name}"

    def _get_local_timezone(self):
        """获取电脑当前的本地时区"""
        return self._local_tz

    def _str_to_utc_timestamp(self, time_str):
        """
        【翻译器】: "本地时间字符串" -> "UTC时间戳"
        核心逻辑: 假设存档里的时间是基于当前电脑时区的，将其转为绝对的UTC时间戳。
        """
        try:
            # 1. 解析字符串为 datetime 对象 (naive time)
            dt_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            # 2. 强行给它贴上“当前电脑时区”的标签
            dt_local = dt_naive.replace(tzinfo=self._get_local_timezone())
            # 3. 转换为 UTC 时间戳 (float)
            return dt_local.timestamp()
        except:
            return 0.0

    def _calculate_server_reset_timestamp(self, strategy_name):
        """
        【计算器】: 计算游戏服务器的上一次刷新时间 (UTC戳)
        无论你在地球哪里，这个函数算出的 绝对时间点 都是一致的。
        """
        # 1. 尝试获取指定策略
        config = CYCLE_STRATEGIES.get(strategy_name)

        # 2. 如果没找到（或者 key 写错了），尝试用第一个可用的策略兜底
        if not config:
            # 获取字典里的第一个 key 作为兜底，防止 crash
            fallback_key = next(iter(CYCLE_STRATEGIES))
            config = CYCLE_STRATEGIES[fallback_key]
            utils.mfaalog.warning(f"[Py] ⚠️ 策略 '{strategy_name}' 未定义，已降级使用 '{fallback_key}'")
        
        # 3. 构造游戏服务器的“现在时间”
        server_tz_offset = config.get("timezone", 8)
        server_tz = timezone(timedelta(hours=server_tz_offset))
        now_server = datetime.now(server_tz)

        # 4. 解析基准刷新点 (例如 04:00)
        # 注意:h/m 一律当作"距当日 0 点的偏移量"参与 timedelta 运算,而不是塞进
        # datetime.replace(hour=...)。replace 只接受 0..23,配置里写 "24:00"
        # (意为当日结束/次日0点)会抛 ValueError,被外层 except 兜住后 return True,
        # 表现为该策略的冷却检查永远直接放行 —— golden_pvp 就踩过这个坑。
        # 用偏移量语义后 "24:00" 自然溢出到次日,甚至能表达 "28:00"=次日4点。
        h, m = map(int, config.get("reset_time", "04:00").split(':'))
        
        cycle_type = config.get("type", "daily")

        # --- 间隔模式 (Interval) 逻辑 v6.0 新增 ---
        if cycle_type == "interval":
            anchor_str = config.get("anchor_date", "2024-01-01")
            interval = config.get("interval_days", 14)
            
            # 构造锚点时间 (带时区)
            anchor_naive = datetime.strptime(anchor_str, "%Y-%m-%d")
            anchor_dt = anchor_naive.replace(tzinfo=server_tz) + timedelta(hours=h, minutes=m)
            
            # 算出 锚点 到 现在 过去了多少天
            delta = now_server - anchor_dt
            
            # 向下取整算出经过了多少个周期
            cycles_passed = int(delta.total_seconds() // (interval * 86400))
            
            # 算出最近的一次刷新时间
            final_reset = anchor_dt + timedelta(days=cycles_passed * interval)
            
            return final_reset.timestamp(), config

        # --- 以下是常规逻辑 ---
        
        # 构造今天的刷新点
        day_start = now_server.replace(hour=0, minute=0, second=0, microsecond=0)
        base_reset = day_start + timedelta(hours=h, minutes=m)
        
        # 如果还没到今天的刷新点，说明上一次刷新是在昨天
        if now_server < base_reset:
            base_reset -= timedelta(days=1)

        # 根据周期类型回溯 (找最近的一个刷新日)
        if cycle_type == "weekly":
            target_wd = config.get("reset_weekday", 0)
            current_wd = base_reset.weekday()
            days_diff = (current_wd - target_wd) % 7
            final_reset = base_reset - timedelta(days=days_diff)

        elif cycle_type == "semi_monthly":
            target_days = config.get("reset_days", [1, 16])
            target_days.sort(reverse=True) # 从大到小排
            
            # 简单粗暴回溯法：从今天往前推，直到撞上刷新日
            check_date = base_reset
            found = False
            for _ in range(32): # 最多往前找一个月
                if check_date.day in target_days:
                    # 还需要确保这个刷新点确实在 now 之前
                    final_reset = check_date
                    found = True
                    break
                check_date -= timedelta(days=1)
            
            if not found: final_reset = base_reset # 兜底

        else: # daily
            final_reset = base_reset

        return final_reset.timestamp(), config

    def check_availability(self, argv):
        # --- 1. 参数解析 ---
        try:
            if hasattr(argv, 'custom_action_param'):
                param_str = getattr(argv, 'custom_action_param', '{}')
                params = json.loads(param_str) if isinstance(param_str, str) else param_str
            elif isinstance(argv, dict):
                params = argv
            else:
                params = {}
            
            card_name = params.get("card_name", "Unknown_Card")
            strategy_name = params.get("cycle_type", "g_weekly") 
        except Exception as e:
            utils.mfaalog.error(f"[Py] 参数解析失败: {e}")
            return True 

        # --- 2. 读取数据库 ---
        storage_key = self._get_storage_key(card_name, strategy_name)
        last_run_str = PersistentStore.get(storage_key, None)

        # --- 3. 计算服务器刷新时间 ---
        try:
            reset_ts, config = self._calculate_server_reset_timestamp(strategy_name)
        except Exception as e:
            # 注意这是"失败开放":算不出刷新点就当作可运行。方向本身有争议
            # (冷却管理器失效时更该保守跳过),但改它会影响所有策略,留待统一评估。
            # 眼下至少把现场留全 —— 此前只打一行 {e},golden_pvp 的 24:00 崩溃
            # 就是这样被压成一句"策略计算异常"、查不出根因的。
            import traceback
            utils.mfaalog.error(f"[Py] 策略计算异常({strategy_name}): {e}")
            for line in traceback.format_exc().rstrip().splitlines():
                utils.mfaalog.error(f"[Py]   {line}")
            return True

        # --- 4. 结算期逻辑 ---
        blackout_min = config.get("blackout_minutes", 0)
        current_ts = time.time()
        settlement_end_ts = reset_ts + (blackout_min * 60)
        
        # 格式化: 只显示 "月-日 时:分" 以节省空间
        local_reset_str = datetime.fromtimestamp(reset_ts).strftime("%Y-%m-%d %H:%M:%S")
        
        if reset_ts <= current_ts < settlement_end_ts:
            end_str = datetime.fromtimestamp(settlement_end_ts).strftime("%H:%M")
            utils.mfaalog.warning(f"\n[Py] ⛔ {card_name} 处于结算期 (至 {end_str})")
            return False

        # --- 5. 核心比对与日志构建 ---
        status_icon = "❓"
        is_pass = False
        last_run_display = "新任务" # 默认显示

        if last_run_str is None:
            # 无记录 -> 通过
            status_icon = "🟢"
            is_pass = True
        else:
            # 有记录 -> 比对
            last_run_ts = self._str_to_utc_timestamp(last_run_str)
            
            # 直接使用完整时间字符串，不再截断
            last_run_display = last_run_str

            if last_run_ts < reset_ts:
                status_icon = "🟢"
                is_pass = True
            else:
                status_icon = "🔴"
                is_pass = False

        # --- 6. 最终整合打印 (单行 + 前置换行) ---
        # 格式: [空行] [图标] 名称(对齐) | 策略 | 基准时间 | 上次时间 -> 结果
        # :<14 表示左对齐占14个字符位，让竖线尽量对齐
        log_msg = f"检查: {card_name:<14}（策略：{strategy_name}） \n 刷新基准: {local_reset_str} \n 上次运行: {last_run_display}"
        
        if is_pass:
            utils.mfaalog.info(f"{log_msg}\n   -> {status_icon} 启动")
            return True
        else:
            # 如果你想在UI上也看到跳过信息，用 info；如果只想在文件里看，用 print 或 debug
            # 这里为了满足你的需求（看到保留的信息），使用 info
            print(f"{log_msg}\n   -> {status_icon} 跳过")
            return False

    def mark_complete(self, argv):
        # --- 参数解析 ---
        try:
            if hasattr(argv, 'custom_action_param'):
                param_str = getattr(argv, 'custom_action_param', '{}')
                params = json.loads(param_str) if isinstance(param_str, str) else param_str
            elif isinstance(argv, dict):
                params = argv
            else:
                params = {}
        except Exception as e:
            utils.mfaalog.error(f"[Py] 参数解析失败: {e}")
            return False

        # --- 统一标准化为列表 ---
        # 目标列表，存放 {'card_name':..., 'cycle_type':...} 字典
        task_list = []

        # 情况 A: 批量模式 (传入了 targets 数组)
        if "targets" in params and isinstance(params["targets"], list):
            task_list = params["targets"]
        
        # 情况 B: 单个模式 (兼容旧写法，直接在根目录有 card_name)
        elif "card_name" in params:
            task_list = [params]
            
        # 如果列表为空，报错
        if not task_list:
            utils.mfaalog.warning("[Py] MarkComplete 未找到有效的 card_name 或 targets 参数")
            return False

        # --- 批量执行保存 ---
        # 获取当前时间 (所有任务统一使用同一个完成时间)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success_count = 0

        for item in task_list:
            # 提取名称，如果没有 card_name 则跳过该项
            c_name = item.get("card_name")
            if not c_name:
                continue
                
            # 提取类型，默认为 g_weekly
            s_name = item.get("cycle_type", "g_weekly")

            # 生成 Key 并写入
            storage_key = self._get_storage_key(c_name, s_name)
            PersistentStore.set(storage_key, now_str)
            success_count += 1
            
            # 打印单条详细日志 (可选)
            utils.mfaalog.debug(f"[Py] 标记更新: {storage_key}")

        # --- 最终日志 ---
        if success_count > 0:
            utils.mfaalog.info(f"[Py] ✅ 批量标记完成: 已更新 {success_count} 个任务的时间戳 -> {now_str}")
            return True
        else:
            return False

manager = CooldownManager()

# Action 注册 (无需修改)
@AgentServer.custom_action("CheckCoolDown")
class CheckCoolDownAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        return manager.check_availability(argv)

@AgentServer.custom_action("MarkComplete")
class MarkCompleteAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        return manager.mark_complete(argv)
    
@AgentServer.custom_recognition("CheckCoolDown")
class CheckCoolDownRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        try:
            # 1. 获取参数 (Recognition 的参数名为 custom_recognition_param)
            params = json.loads(argv.custom_recognition_param)
            
            # 2. 调用核心逻辑 (传入解析后的字典)
            is_available = manager.check_availability(params)
            
            # 3. 根据结果返回
            if is_available:
                # 🟢 任务可用 -> 返回 AnalyzeResult (逻辑上的“识别成功”)
                # 这里的 detail 可以传递给后续的 Action (如果有)
                msg = f"任务 {params.get('card_name')} 可执行"
                return CustomRecognition.AnalyzeResult(
                    box=[0, 0, 0, 0],  # 虚拟坐标，逻辑检查不需要真实坐标
                    detail={"msg": msg, "card_name": params.get('card_name')}
                )
            else:
                # 🔴 任务冷却中 -> 返回 None (逻辑上的“识别失败/跳过”)
                return None
                
        except Exception as e:
            utils.mfaalog.error(f"[Py] CheckCoolDownRecognition 异常: {e}")
            return None