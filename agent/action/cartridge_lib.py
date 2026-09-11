import json
import os
import time
from datetime import datetime, timedelta, timezone
import utils

# ✅ 引入存档工具
# (不需要改 persistent_store.py，它负责底层读写，这里负责业务逻辑)
from utils.persistent_store import PersistentStore
from utils.account_sync import sync_from_context

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
# 失败开放：参数解析失败 / 策略算不出刷新点 / 存档时间串读不懂 —— 这三类判不出来的
#           情况一律**当作可运行放行**，并按 error 级留日志(不再静默)。写节点时要知道
#           异常态是"放行"而不是"拦截"。方向本身有争议，但三处一致，要改得一起评估。
#
# ------------------------------------------------------------------------------
# 📝 JSON Pipeline 配置指南 (标准规范版)
# ------------------------------------------------------------------------------
#
# ⚠️ 判据只有一个方向："冷却已过 = 识别成功"。
#    想表达"已完成才通过"(比如"整批都刷完了就收尾")不能靠 And 组合多个本识别器 ——
#    三个都命中意味着三个都**还没做**，正好相反；节点级 inverse 也不行，它反转的是
#    整个组合式，NOT(A且B且C) 会变成"任一已完成即命中"。那种判据请看【模式 C】。
#
# 【模式 A】作为“自定义识别器”使用 (推荐 ⭐)
# ---------------------------------------------------
# 逻辑：冷却已过 -> 视为“识别成功”，执行当前节点的 action。
#       冷却未过 -> 视为“识别失败”，父节点跳过这个候选，去试 next 里的下一个。
#
# "Task_Daily_Dungeon": {
#     "recognition": "Custom",               // ⚡️ 必须固定为 Custom
#     "custom_recognition": "CheckCoolDown", // ⚡️ 指向 Python 注册的 ID
#     "custom_recognition_param": {
#         "card_name": "Map_01",             // 任务唯一标识 ID
#         "cycle_type": "g_daily"            // 策略类型，缺省为 g_weekly
#     },
#     "pre_delay": 0,                        // 纯逻辑判定不看画面，这两个写 0
#     "post_delay": 0,                       // (协议默认各 200ms，省略就白吃)
#     "action": "Click",
#     "target": [ 640, 360 ],
#     "next": [ "Sub_Task" ]
# }
#
# ⚠️ **不要写 `rate_limit: 0`。** 它管的不是本节点的延迟，而是**上游重试识别本节点的
#    最小间隔**。上游在 timeout 窗口内会反复识别 next 候选，写 0 会让空转变成满速刷，
#    每一轮都是一次 Python IPC 往返。默认 1000ms 正合适，别动。
#
# ⚠️ **上游节点要配 `timeout`。** 调用本闸的那个节点若 `next` 里只剩本闸一项，闸不命中时
#    它会空转到默认 20 秒才退栈。队列 hub 的惯例取值是 `"timeout": 1`（1ms 超时静默出栈），
#    照抄 `Daily_Union_Hub` / `Daily_VisitCabin_Hub`。**不要写 0**，全库无先例且协议未定义。
#
# ⚠️ 识别不中走的是"父节点换下一个候选"，**不是**这个节点自己的 on_error。
#    on_error 是本节点超时或动作失败时才走的，别指望拿它接"冷却中"的分支 ——
#    想在冷却中做别的事，把那件事写成 next 列表里排在后面的另一个候选。
#
# 【模式 B】作为“自定义动作”使用
# ---------------------------------------------------
# 逻辑：检查通过 -> Action 返回 True，继续 next。
#       检查不通过 -> Action 返回 False，节点进入错误态(没写 on_error 就静默出栈)。
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
# 【模式 C】批量总闸 (targets + match)
# ---------------------------------------------------
# 一次问一批"还有没有活"，只返回一个布尔值。用途是省掉"整批都做完了、却仍逐个
# 空转识别"的开销 —— 候选多时这笔开销是每轮都要付的。
#
# ⚠️ 它**不返回**"哪几项可跑"。CustomAction 读不到识别的 detail，身份信息传不下去。
#    要按项分发仍然得靠 pipeline 把候选展开成多个节点；批量只适合做总闸。
#
# "Collect_AllLibGone": {
#     "recognition": "Custom",
#     "custom_recognition": "CheckCoolDown",
#     "custom_recognition_param": {
#         "targets": [                       // ← 有 targets 就走批量，否则走单个
#             { "card_name": "Pack_Story_SimpleDone", "cycle_type": "g_weekly" },
#             { "card_name": "Pack_Event_SimpleDone", "cycle_type": "g_weekly" }
#         ],
#         "match": "all_done"                // ← 见下表，缺省 "any"
#     },
#     "pre_delay": 0,
#     "post_delay": 0,
#     "focus": "全类已完成,收尾"
#     // 不写 next = 命中后退栈收尾；要硬停整个 task 才写 "action": "StopTask"
# }
#
# match 三种取值（"可跑" = 冷却已过）：
#   any       至少一项可跑   -> 正向入口闸："还有活干才进这个模块"
#   none      没有一项可跑   -> 总闸："没活了"。结算期、判定异常都算进"不可跑"
#   all_done  全部已完成     -> 严格总闸：结算期与判定异常都不算 done
#
# ⚠️ none 与 all_done 的区别值得留意："不可跑"有两种原因 —— 已完成、和正处在结算
#    保护期。拿 none 当"全完成"判据时，只是卡在结算期的项也会被算进去。
#    判"真的都做完了"一律用 all_done。(blackout_minutes 为 0 的策略两者等价)
#
# ⚠️ 配置错(targets 非数组/为空/match 拼错/项缺 card_name)一律"不命中" + error 日志。
#    这在两种 match 下安全性不对称：none 当总闸时不命中 = 照常逐个跑，不会漏任务；
#    any 当入口闸时不命中 = 整个模块被跳过，会漏任务。
#    排查搜日志里的 "❌ CheckCoolDown 批量模式"。
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

        解析不出来时返回 None,而不是 0.0。0.0 本身是个合法时间戳(1970-01-01),
        与"解析失败"混在一起后调用方无从区分 —— 它恒小于任何 reset_ts,于是
        "打过标"被静默判成"从没跑过",表现为完成态下反复重跑,且日志里一个字都没有。
        返回 None 把这两件事拆开,由调用方显式决定怎么收场。
        """
        try:
            # 1. 解析字符串为 datetime 对象 (naive time)
            dt_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            # 2. 强行给它贴上“当前电脑时区”的标签
            dt_local = dt_naive.replace(tzinfo=self._get_local_timezone())
            # 3. 转换为 UTC 时间戳 (float)
            return dt_local.timestamp()
        except (ValueError, TypeError) as e:
            # ValueError: 格式对不上(存档被手改过、或将来换了存储格式)
            # TypeError : 存档里根本不是字符串(JSON 里存成了数字/null)
            # 原先是裸 except —— 连 KeyboardInterrupt / SystemExit 都一并吞掉。
            # 不必再捕 OSError/OverflowError: 上面 replace(tzinfo=...) 之后是 aware
            # datetime,其 timestamp() 走纯算术而非平台 mktime,1970 年之前也不报错
            # (实测 "1960-01-01 00:00:00" 正常返回 -315597600.0)。别再往回补。
            utils.mfaalog.error(
                f"[Py] ❌ 存档时间戳无法解析: {time_str!r} ({type(e).__name__}: {e})"
            )
            return None

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

    # 单卡判定的四种状态。批量聚合(_check_batch)按这四态统计,别在调用处直接写
    # 字符串字面量 —— 拼错了会静默落进 else 分支。
    STATE_RUNNABLE = "runnable"   # 冷却已过,可以跑
    STATE_DONE = "done"           # 本周期内已打过标
    STATE_BLOCKED = "blocked"     # 处于结算保护期,这一刻不能碰
    STATE_ERROR = "error"         # 判不出来(策略算不出),沿用失败开放当可跑

    def _check_one(self, card_name, strategy_name, quiet=False, store=None, reset_cache=None):
        """判定单张卡的冷却状态。

        check_availability(单卡) 与 _check_batch(批量) 共用这一份比对逻辑,
        拆出来是为了让批量判据不必复制一遍。

        quiet=True 时不打结算期的 warning —— 批量下 30 多张卡同时处于结算期
        会刷屏,汇总日志里有计数。

        store / reset_cache 只在批量下由 _check_batch 传入:
        · store       已 load 好的存档快照。PersistentStore.get() 每次都会读盘
                      并解析 JSON,33 张卡就是 33 次文件 IO,快照把它压成 1 次。
        · reset_cache 同一 strategy_name 的刷新点在一批内是同一个值,算一次即可。
                      值为 None 表示这个策略上面已经算崩过,别再重复打一遍堆栈。

        Returns
        -------
        dict: state 为上面四个 STATE_* 之一; icon/reset/last 供日志展示。
        """
        # --- 读取数据库 ---
        storage_key = self._get_storage_key(card_name, strategy_name)
        if store is not None:
            last_run_str = store.get(storage_key, None)
        else:
            last_run_str = PersistentStore.get(storage_key, None)

        # --- 计算服务器刷新时间 ---
        if reset_cache is not None and strategy_name in reset_cache:
            cached = reset_cache[strategy_name]
            if cached is None:
                # 这一批里已经为该策略打过堆栈了,直接沿用同一个失败结论
                return {"state": self.STATE_ERROR, "icon": "❓", "reset": "-", "last": "-"}
            reset_ts, config = cached
        else:
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
                if reset_cache is not None:
                    reset_cache[strategy_name] = None
                return {"state": self.STATE_ERROR, "icon": "❓", "reset": "-", "last": "-"}
            if reset_cache is not None:
                reset_cache[strategy_name] = (reset_ts, config)

        # --- 结算期逻辑 ---
        blackout_min = config.get("blackout_minutes", 0)
        current_ts = time.time()
        settlement_end_ts = reset_ts + (blackout_min * 60)
        local_reset_str = datetime.fromtimestamp(reset_ts).strftime("%Y-%m-%d %H:%M:%S")

        if reset_ts <= current_ts < settlement_end_ts:
            if not quiet:
                end_str = datetime.fromtimestamp(settlement_end_ts).strftime("%H:%M")
                utils.mfaalog.warning(f"\n[Py] ⛔ {card_name} 处于结算期 (至 {end_str})")
            return {"state": self.STATE_BLOCKED, "icon": "⛔",
                    "reset": local_reset_str, "last": "-"}

        # --- 核心比对 ---
        if last_run_str is None:
            # 无记录 -> 通过
            return {"state": self.STATE_RUNNABLE, "icon": "🟢",
                    "reset": local_reset_str, "last": "新任务"}

        last_run_ts = self._str_to_utc_timestamp(last_run_str)
        if last_run_ts is None:
            # 有记录、但那条记录读不懂。沿用本文件既有的"失败开放"方向(当作可运行),
            # 与上面算不出刷新点时的兜底保持一致 —— 方向本身有争议(冷却管理器
            # 失效时更该保守跳过),但改它要连着那几处一起评估,这里只负责别再静默。
            # _str_to_utc_timestamp 已按 error 级记了原始值与异常类型。
            return {"state": self.STATE_RUNNABLE, "icon": "🟡", "reset": local_reset_str,
                    "last": f"{last_run_str!r} ← 解析失败,已按未运行处理"}
        if last_run_ts < reset_ts:
            return {"state": self.STATE_RUNNABLE, "icon": "🟢",
                    "reset": local_reset_str, "last": last_run_str}
        return {"state": self.STATE_DONE, "icon": "🔴",
                "reset": local_reset_str, "last": last_run_str}

    # match 取值 -> 一句话语义。写错时报错而不是静默套默认值。
    # 刻意没有 "all"(全部可跑才命中) —— 想不出用途,要用再加。
    MATCH_MODES = {
        "any":      "至少一项可跑",
        "none":     "没有一项可跑(结算期、判定异常都算进来)",
        "all_done": "全部已完成(结算期与判定异常都不算 done)",
    }

    def _check_batch(self, params):
        """批量聚合判据:一次问一批卡带"还有没有活",而不是逐个节点各问一次。

        用途是给 pipeline 侧做"总闸"。实测 Collect_PackLocation_PassFieldsAndHub
        的 32 个候选全刷完后仍会逐个空转,一轮约 2.7s,一次运行里空转了 22 轮共
        约 62s —— 这些轮次里 And 因短路只发了 1 次 IPC,开销主体是 31 次模板匹配,
        所以省不掉,只能靠总闸整轮跳过。

        ⚠️ 这里**不返回**"哪几张可跑"。CustomAction 拿不到识别的 detail,身份信息
        传不下去;能传下去的只有这一个布尔值。要按卡分发仍然得靠 pipeline 侧把
        候选展开成多个节点(现有 33 个 And 节点就是这么做的),批量只适合做总闸。
        """
        targets = params.get("targets")
        match = params.get("match", "any")

        # ⚠️ 配置错一律返回"不命中"。这在两种 match 下的安全性并不对称:
        #    match=none 当总闸时,不命中 = 闸不成立 = 照常逐个跑,不会漏做任务;
        #    match=any  当入口闸时,不命中 = 整个模块被跳过,会漏做任务。
        # 所以下面每一条都配 error 级日志,别让它悄悄退化。
        if not isinstance(targets, list) or not targets:
            utils.mfaalog.error(
                f"[Py] ❌ CheckCoolDown 批量模式: targets 必须是非空数组,实际为 {targets!r}"
            )
            return False

        if match not in self.MATCH_MODES:
            utils.mfaalog.error(
                f"[Py] ❌ CheckCoolDown 批量模式: match 取值非法 {match!r},"
                f"可选 {list(self.MATCH_MODES)}"
            )
            return False

        counts = {self.STATE_RUNNABLE: 0, self.STATE_DONE: 0,
                  self.STATE_BLOCKED: 0, self.STATE_ERROR: 0}
        runnable_names = []
        bad_items = 0

        # 整批共用一份存档快照与策略缓存,见 _check_one 的 docstring。
        # 快照是本次判定的一致视图 —— 期间没有写入,不存在读到半旧半新的问题。
        store = PersistentStore.load()
        reset_cache = {}

        for item in targets:
            if not isinstance(item, dict) or not item.get("card_name"):
                bad_items += 1
                continue
            c_name = item["card_name"]
            s_name = item.get("cycle_type", "g_weekly")
            r = self._check_one(c_name, s_name, quiet=True,
                                store=store, reset_cache=reset_cache)
            counts[r["state"]] += 1
            if r["state"] == self.STATE_RUNNABLE:
                runnable_names.append(c_name)
            utils.mfaalog.debug(f"[Py]   · {c_name}@{s_name} -> {r['state']} (上次 {r['last']})")

        if bad_items:
            # 手写三十多项漏个 card_name 太容易了。静默跳过会让闸的判据
            # 悄悄少算几张卡,而少算的方向恰好是"看起来更像全完成了"。
            utils.mfaalog.error(
                f"[Py] ❌ CheckCoolDown 批量模式: {bad_items} 项缺少 card_name 已跳过,请检查节点参数"
            )

        checked = sum(counts.values())
        if checked == 0:
            utils.mfaalog.error("[Py] ❌ CheckCoolDown 批量模式: targets 里没有一项有效目标")
            return False

        if match == "any":
            hit = counts[self.STATE_RUNNABLE] > 0
        elif match == "none":
            hit = counts[self.STATE_RUNNABLE] == 0
        else:  # all_done
            hit = counts[self.STATE_DONE] == checked

        preview = "、".join(runnable_names[:5])
        if len(runnable_names) > 5:
            preview += f" …共 {len(runnable_names)} 项"
        utils.mfaalog.info(
            f"[Py] 📋 批量冷却检查 {checked} 项 (match={match}: {self.MATCH_MODES[match]})\n"
            f"      可跑 {counts[self.STATE_RUNNABLE]} / 已完成 {counts[self.STATE_DONE]}"
            f" / 结算期 {counts[self.STATE_BLOCKED]} / 判定异常 {counts[self.STATE_ERROR]}"
            + (f"\n      可跑: {preview}" if runnable_names else "")
            + f"\n   -> {'✅ 命中' if hit else '⬜ 不命中'}"
        )
        return hit

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
        except Exception as e:
            utils.mfaalog.error(f"[Py] 参数解析失败: {e}")
            return True

        # params 未必是 dict:custom_*_param 写成数组时 json.loads 出来就是 list,
        # 下面的 .get 会 AttributeError。原先靠外层 try 兜住,拆分后要显式挡一道。
        if not isinstance(params, dict):
            utils.mfaalog.error(
                f"[Py] 参数应为对象,实际为 {type(params).__name__}: {params!r}"
            )
            return True

        # --- 2. 批量模式:targets 数组 + match 聚合判据 ---
        # 与 MarkComplete 的 targets 写法对齐;不带 targets 时走下面的单卡老路。
        if "targets" in params:
            return self._check_batch(params)

        # --- 3. 单卡模式 ---
        card_name = params.get("card_name", "Unknown_Card")
        strategy_name = params.get("cycle_type", "g_weekly")

        r = self._check_one(card_name, strategy_name)
        if r["state"] == self.STATE_ERROR:
            return True    # 失败开放,与拆分前一致:算不出刷新点就当作可运行
        if r["state"] == self.STATE_BLOCKED:
            return False   # 结算期的 warning 已在 _check_one 里打过

        # --- 4. 最终整合打印 (单行 + 前置换行) ---
        # 格式: [空行] [图标] 名称(对齐) | 策略 | 基准时间 | 上次时间 -> 结果
        # :<14 表示左对齐占14个字符位，让竖线尽量对齐
        log_msg = (f"检查: {card_name:<14}（策略：{strategy_name}） \n"
                   f" 刷新基准: {r['reset']} \n 上次运行: {r['last']}")

        if r["state"] == self.STATE_RUNNABLE:
            utils.mfaalog.info(f"{log_msg}\n   -> {r['icon']} 启动")
            return True
        else:
            # 如果你想在UI上也看到跳过信息，用 info；如果只想在文件里看，用 print 或 debug
            # 这里为了满足你的需求（看到保留的信息），使用 info
            print(f"{log_msg}\n   -> {r['icon']} 跳过")
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

# ==============================================================================
# 存档号同步的 pull 路径
# ==============================================================================
# 本文件是全仓唯一实际读写存档的业务代码（其余 PersistentStore 调用点只是
# main.py 的启动挂载与 SwitchAccountCheckpoint 的 push 同步）。所以在这三个
# 注册入口各同步一次，就覆盖了全部存档读写 —— 用户从哪个 task 起跑都不会用错档，
# 不必依赖 `Env_AccountSave_Switch` 节点被执行。
#
# sync_from_context 承诺永不抛异常，且值相同时零副作用，可以裸调。
# 详见 utils/account_sync.py。
# ==============================================================================

@AgentServer.custom_action("CheckCoolDown")
class CheckCoolDownAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        sync_from_context(context, where="CheckCoolDown/action")
        return manager.check_availability(argv)

@AgentServer.custom_action("MarkComplete")
class MarkCompleteAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        sync_from_context(context, where="MarkComplete")
        return manager.mark_complete(argv)

@AgentServer.custom_recognition("CheckCoolDown")
class CheckCoolDownRecognition(CustomRecognition):
    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        sync_from_context(context, where="CheckCoolDown/reco")
        try:
            # 1. 获取参数 (Recognition 的参数名为 custom_recognition_param)
            params = json.loads(argv.custom_recognition_param)
            
            # 2. 调用核心逻辑 (传入解析后的字典)
            is_available = manager.check_availability(params)
            
            # 3. 根据结果返回
            if is_available:
                # 🟢 判据成立 -> 返回 AnalyzeResult (逻辑上的“识别成功”)
                # detail 会原样进 maafw.log 的 reco_details,排查时能看到判据依据;
                # 但 CustomAction 读不到它,别指望用它往下游传数据。
                if isinstance(params, dict) and "targets" in params:
                    n = len(params.get("targets") or [])
                    mode = params.get("match", "any")
                    detail = {"msg": f"批量判据成立 (match={mode}, {n} 项)",
                              "match": mode, "count": n}
                else:
                    card = params.get('card_name') if isinstance(params, dict) else None
                    detail = {"msg": f"任务 {card} 可执行", "card_name": card}
                return CustomRecognition.AnalyzeResult(
                    box=[0, 0, 0, 0],  # 虚拟坐标，逻辑检查不需要真实坐标
                    detail=detail
                )
            else:
                # 🔴 判据不成立 -> 返回 None (逻辑上的“识别失败/跳过”)
                return None

        except Exception as e:
            # 这里返回 None 会让上层看成"界面上没有",与真正的冷却中不可区分,
            # 所以堆栈必须留全 —— 与 _check_one 里策略计算异常的处理保持一致。
            import traceback
            utils.mfaalog.error(f"[Py] CheckCoolDownRecognition 异常: {e}")
            for line in traceback.format_exc().rstrip().splitlines():
                utils.mfaalog.error(f"[Py]   {line}")
            return None