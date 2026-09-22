# -*- coding: utf-8 -*-
"""
存档号同步 (Account Sync)
=========================
把「当前该用哪个存档」从「等某个节点被执行」改成「要用之前自己看一眼」。

为什么不需要扳机
----------------
存档号由 interface.json 的「存档名称」选项经 pipeline_override 注入
`Env_AccountSave_Switch` 的 custom_action_param。该选项挂在 global_option 下，
MFAAvalonia 会把它合并进**每一个** task 的 pipeline override —— 也就是说任何
task 运行时这个节点上的 account_id 都已是最新值，**无需该节点被执行**
（内核 get_node_data 先查 override 表再查资源，见 Context.cpp）。

而 PersistentStore 只可能被 py 调用，py 只在 CustomAction/CustomRecognition 里跑
⇒ 存档的任何读写都必然发生在某个 custom 内部。所以在各业务入口同步一次，
就覆盖了全部存档读写，不碰 custom 的 task 也不碰存档，漏了无妨。

两条路共用本模块，互相幂等：
  · push —— `Env_AccountSave_Switch` 被执行时（StartGame 链首，早期同步 + 日志）
  · pull —— cartridge_lib、arbitrage_result、cooking_stock 的 custom 入口，每次读写账号存档前

幂等由 PersistentStore.switch_account 保证：它按值比较，相同则什么都不做，
不打日志、不重挂路径。所以"每次都同步"的成本约等于零。

失败时为什么不拦截业务
----------------------
PR #478 的两个机审工具都提过同一条：同步失败时保留旧存档号，后续 CheckCoolDown /
MarkComplete 会把当前 task 的进度写进上一个 task 用的存档，建议同步失败就返回 False、
不执行业务。这条**不采纳**，理由有三：

1. 它的前提（"某个 task 缺少该节点"）不成立。`Env_AccountSave_Switch` 定义在
   `assets/resource/base/pipeline/Dummy.json`，是**资源级**节点，随 base 资源包全局
   加载；pipeline_override 只改它的值，不决定它存不存在。而内核
   `Context::get_pipeline_data`（Context.cpp:324-350）查不到 override 会**回落到资源**。
   所以任何 task 都读得到，最差是读到资源里的默认 `{"account_id": "0"}`。
2. 于是本函数返回 False 只可能是**全局性**故障（节点被删/改名、get_node_data API 变了）。
   那种情况下第一个 task 就会失败，_current_account_id 还停在启动默认值，
   压根构造不出机审设想的"A 成功、B 失败"组合。
3. 真拦截了，后果更糟且更难查：CheckCoolDown 返回未命中会让父节点跳过候选、整个模块
   静默跳过；MarkComplete 返回 False 会让节点进错误态静默出栈 —— 任务做了却没标记，
   下次重复做。而这与 cartridge_lib 既有的「失败开放」哲学相反（见该文件头部注释，
   那里明确写了三处一致、要改得一起评估）。

⚠️ 一个机审没提、但方向相反且更真实的风险：若「存档名称」的 override 因故**没有**被
合并进某个 task，本函数会读到资源里的默认值 "0" 并**主动把档切成 0** —— 那才是会实际
发生的串档路径。当前不会触发，因为「存档名称」是 input 类型且**没有 controller /
resource 限制**，MFAAvalonia 的 ProcessOptions 走 input 分支时有默认值兜底、恒生成
override。**所以不要给 interface.json 里的「存档名称」加 controller 或 resource 字段** ——
一旦加了，不适用的那些 task 就会读到默认值 0 而误切档，且没有任何告警。
"""

from . import mfaalog as logger
from .persistent_store import PersistentStore

# 承载存档号的节点。名字变更时必须同步改 assets/resource/base/pipeline/Dummy.json
# 与 interface.json 中「存档名称」的 pipeline_override。
NODE_NAME = "Env_AccountSave_Switch"

# 读取失败的告警去重：每个 custom 入口都会调用本模块，失败时不去重会刷屏。
# 这个 flag 只用于抑制重复告警，**不缓存存档号本身** —— 存档号每次都重新读，
# 否则运行途中改存档号就不再生效（interface.json 里对用户承诺了它会生效）。
_warned = False


def sync_from_context(context, where: str = "") -> bool:
    """从 context 读取当前存档号并同步给 PersistentStore。

    Parameters
    ----------
    context : maa.context.Context
        custom 回调收到的上下文。
    where : str
        调用点标识，仅用于日志定位。

    Returns
    -------
    bool
        True  已读到存档号（无论是否真的发生了切换）
        False 没读到 —— **当前存档号保持不变**

    本函数承诺永不抛异常，调用方可以裸调，不必自己包 try。

    读不到时为什么不回落成 "0"
    --------------------------
    读不到只有两种可能：节点被删/改名，或框架 API 变了。两种都是真问题。
    此时若"降级成 0 号档"，会把本该写进 N 号档的数据写进公共档 —— 比不切换
    危险得多，且是静默的。所以失败路径是「什么都不做 + 告警」，不是「用默认值」。
    """
    global _warned

    try:
        node = context.get_node_data(NODE_NAME)

        if not node:
            _warn_once(
                f"[Py] ⚠️ 节点 {NODE_NAME} 不存在或读取为空{_suffix(where)}，"
                f"存档号维持当前值不变。请检查该节点是否被改名或删除。"
            )
            return False

        # get_node_data 返回 **V2 归一化**结构：custom_action_param 在 action.param
        # 下，顶层没有（与 roi 只在 recognition.param 里同构，按顶层读会静默拿到 None）。
        action = node.get("action")
        param = action.get("param") if isinstance(action, dict) else None
        custom_param = param.get("custom_action_param") if isinstance(param, dict) else None

        if not isinstance(custom_param, dict) or "account_id" not in custom_param:
            _warn_once(
                f"[Py] ⚠️ 节点 {NODE_NAME} 未携带 custom_action_param.account_id"
                f"{_suffix(where)}，存档号维持当前值不变。"
            )
            return False

        # 相同则 switch_account 内部直接返回，不会有任何副作用与日志输出。
        PersistentStore.switch_account(custom_param["account_id"])
        _warned = False
        return True

    except Exception as e:
        _warn_once(f"[Py] ⚠️ 存档号同步失败{_suffix(where)}: {e}")
        return False


def _suffix(where: str) -> str:
    return f" (来自 {where})" if where else ""


def _warn_once(msg: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(msg)
