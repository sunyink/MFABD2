# -*- coding: utf-8 -*-
"""Pipeline 动态管理器 —— 运行时改写 / 还原 MaaFramework 管线节点。"""

import copy
import json
import re

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

import utils
from recognition.counter import TAG_STORE

__version__ = "2.1.0"

# ==============================================================================
# Pipeline 动态管理器  v2.1.0
# ==============================================================================
# 提供两个 Custom Action：
#
#   PatchPipeline    运行时改写节点参数，并自动登记还原点
#   RestorePipeline  把改过的节点还原回去
#
# ------------------------------------------------------------------------------
# 设计要点：为什么不用手写 origin
# ------------------------------------------------------------------------------
# 旧版要求调用方在 JSON 里手抄一份 "origin"（原值）用于还原。这有三个问题：抄错了
# 无人校验；抄的是磁盘上的静态值，而运行时真实生效的值还叠加了 UI 选项覆盖；节点改了
# 之后 origin 不会跟着改。
#
# 本版改为：打补丁前后各向框架查询一次节点的**当前定义**，两次结果做差，差出来的部分
# 就是还原点。框架是唯一权威 —— 取值、V1/V2 格式转译、字段合并全部由它完成，本模块
# 只做纯字典比较。调用方一个字都不用写。
#
# ------------------------------------------------------------------------------
# 关于写法：V1 / V2 都行
# ------------------------------------------------------------------------------
# 下面的示例用 V1 扁平写法（recognition / action 直接是字符串，参数摊在节点顶层），
# 因为这是本项目的习惯。但**两种写法本模块都吃**：
#
#   V1  "action": "Custom", "custom_action": "PatchPipeline", "custom_action_param": {...}
#   V2  "action": { "type": "Custom", "param": { "custom_action": "PatchPipeline",
#                                                "custom_action_param": {...} } }
#
# patch 内容同理 —— 写 V1 的 "expected": [...]，或写 V2 的
# "recognition": {"param": {"expected": [...]}}，都可以。框架自己完成转译，本模块
# 不解析这些字段，只把补丁原样交给框架。
#
# ------------------------------------------------------------------------------
# 1. 打补丁：一个节点
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "PatchPipeline",
# "custom_action_param": {
#     "target": "Battle_Entry",              // [必填] 目标节点名
#     "patch":  { "timeout": 30000 }         // [必填] 要改的字段，按平时写节点的写法写
# }
#
# ------------------------------------------------------------------------------
# 2. 打补丁：一批节点用同一份补丁
# ------------------------------------------------------------------------------
# "custom_action_param": {
#     "target": ["Node_A", "Node_B"],        // 数组
#     "patch":  { "enabled": false }
# }
#
# 也可以用正则选目标（在框架已加载的节点名里搜，支持字符串或数组）：
# "custom_action_param": {
#     "target": { "regex": "^Shop_Buy_Item_\\d+$" },
#     "patch":  { "timeout": 5000 }
# }
#
# ⚠️ 正则零命中 = 硬失败。宁可报错，也不要让"以为改了其实没改"悄悄溜过去。
#
# target 里还能写 "[Anchor]锚点名"（字符串或数组里都行），运行时解析成该锚点当前指向
# 的节点，用来对付「共享结束节点要改的是刚才跳进来的那个节点」这类场景：
# "custom_action_param": {
#     "target": "[Anchor]CurCard",
#     "patch":  { "enabled": false }
# }
#
# ── 目标选择器的三种写法，按序判定一次、选中即定，不能既要还要 ──
#   ① 固定节点名   "Node_A" / ["Node_A", "Node_B"]
#   ② 锚点         "[Anchor]锚点名"   ← 单个名字，运行时才知道是谁
#   ③ 节点名正则   {"regex": "..."}   ← 在已加载节点名里搜一批
# 锚点不支持正则：锚点名里写正则不会被当正则解释，只会拿去当锚点名查，查不到即跳过。
#
# 三者对「没选中任何节点」的处置也不同，别混：
#   · 锚点没指向任何节点  → 跳过该目标，发一条日志，不算失败（流程的正常状态）
#   · 正则零命中          → 硬失败（配置错误）
#   · target 写成空字符串 → 硬失败（写了 target 却没写值，同样是配置错误，
#                            会落到下面「节点不存在」那条报错上）
#
# ------------------------------------------------------------------------------
# 3. 打补丁：每个节点各自不同的补丁
# ------------------------------------------------------------------------------
# target + patch 是「同一份补丁打到一批节点」。如果每个节点要改的东西不一样，用 patches：
#
# "custom_action_param": {
#     "patches": {
#         "Node_A":          { "next": ["Node_X"] },
#         "[Anchor]CurCard": { "enabled": false },   // 键同样认锚点
#         "Node_B":          { "timeout": 3000 }
#     }
# }
#
# 键的解析规则与 target 完全一致（固定节点名 / 锚点二选一）—— 唯一的差别是 patches
# 的键**不支持正则**：正则是"一个补丁打一批节点"，那正是 target+patch 的活。
#
# 之所以要有这个形态：一个节点只能有一种 action，没法靠"多写几个节点"来表达。
# target+patch 与 patches 同时出现 = 硬失败（意图不明，不猜）。
#
# ------------------------------------------------------------------------------
# 4. 还原
# ------------------------------------------------------------------------------
# "custom_action": "RestorePipeline",
# "custom_action_param": { "target": "Battle_Entry" }      // 单个
# "custom_action_param": { "target": ["Node_A","Node_B"] } // 多个
# "custom_action_param": { "target": "*" }                 // 本次任务改过的全部
#
# 还原点由 PatchPipeline 自动登记，采用「先到先得」：同一节点被反复打补丁时，还原点
# 始终是**第一次打补丁之前**的状态。
#
# 还原本身是幂等的 —— 目标不在账本里（没被改过 / 已经还原过）只发一条告警，不算失败。
#
# 如果某次改写是一次性的、压根不打算还原，可以在 PatchPipeline 一侧关掉登记：
#
# "custom_action_param": {
#     "target": "Battle_Entry",
#     "patch":  { "timeout": 30000 },
#     "backup": false                        // 默认 true。false = 不登记还原点
# }
#
# ⚠️ backup: false 的节点不进账本 —— RestorePipeline 点名还原它，会落到「账本中无还原点，
#    已跳过」那条告警；"*" 批量还原也不会覆盖它。它只随任务结束由框架自动失效。
#    这个开关只影响账本，不影响补丁本身，补丁照打不误。
#
# （注意别和旧版 RestoreNode 的同名参数搞混：那个 backup 是「还原时手工传一份备份数据
#  进来」，语义相反，已随旧 API 一并废弃，迁移脚本遇到会丢弃并告警。）
#
# ------------------------------------------------------------------------------
# 5. 旁作用：顺手清计数器、顺手点一下
# ------------------------------------------------------------------------------
# 两个动作都支持。同样是因为一个节点只能有一种 action —— 想「改参数 + 点一下 +
# 清计数器」，不做成参数就得拆三个节点。
#
# "custom_action_param": {
#     "target": "Battle_Entry",
#     "patch":  { "timeout": 30000 },
#     "reset_tags": ["Battle_Count"],            // 把这些计数器清零
#     "click": {}                                // 点当前识别框中心
#     // "click": { "offset": [10, 20, 40, 40] } // 或点 [识别框左上角+dx+dy] 起、w×h 的区域中心
# }
#
# ------------------------------------------------------------------------------
# 6. 新建节点
# ------------------------------------------------------------------------------
# 补丁也可以凭空造一个资源里不存在的节点，但必须显式声明 —— 因为「节点名拼错」和
# 「打算新建」在接口层长得一模一样，不声明就无从区分：
#
# "custom_action_param": {
#     "target": "Tmp_Node",
#     "create": true,                        // 不写 = 节点不存在即报错
#     "patch":  { "recognition": "DirectHit", "action": "Click", "target": [1,2,3,4] }
# }
#
# 新建的节点没有"原状"可还原，因此不进账本；任务结束时框架会自动清掉它。
#
# ⚠️ create 对**锚点目标一律无效**（写了也不生效，直接报错）。锚点指向的必然是流程里
#    刚跑过的既有节点，解析出来却查无此节点，只能是上游 anchor 登记的名字写错了 ——
#    此时若允许 create，就会凭空造一个同名空节点把错误盖住：补丁打在谁也不引用的
#    节点上，动作还返回成功，你想改的那个节点毫发无损。这类问题极难查，故禁掉。
#
# ------------------------------------------------------------------------------
# 7. 占位符：$box / $self / $caller
# ------------------------------------------------------------------------------
# 可以出现在 patch 里的任意位置（不限于某个字段）：
#
#   "$box"     整条替换成当前识别框 [x, y, w, h]
#   "$self"    子串替换成"正在被改的那个节点的名字"（配合正则选择器时有用）
#   "$caller"  子串替换成 caller 参数的值，需要在参数里显式写 "caller": "本节点名"
#
# "custom_action_param": {
#     "caller": "My_Entry_Node",
#     "target": { "regex": "^Enemy_.*$" },
#     "patch":  { "roi": "$box", "next": ["[JumpBack]$caller"] }
# }
#
# ------------------------------------------------------------------------------
# 8. 三个容易踩的坑
# ------------------------------------------------------------------------------
# (1) 数组是整体替换，不是逐元素合并。
#     想改 "all_of" 里第 0 个元素的 roi，必须把整个 all_of 数组写全，只写第 0 个会
#     让其余元素消失。（把条件写成外置节点、在 all_of 里只放节点名，就能绕开这点 ——
#     那时 roi 是被引用节点的一级字段，直接 patch 即可。）
#
# (2) custom_action_param / custom_recognition_param 是整体替换。
#     框架不理解这两个字段的内部结构，所以不做合并。补丁里带上它们就必须写全量，
#     只写想改的那一个键会让其余键丢失。
#
# (3) 补丁的作用范围是**单次任务**。
#     任务结束后框架会自动丢弃所有运行时改写，还原点账本也随之失效 —— 不需要、也
#     无法跨任务还原。
# ==============================================================================


class ConfigError(Exception):
    """调用方在 JSON 里写错了。硬失败走 on_error，不静默降级。"""


# ------------------------------------------------------------------------------
# 还原点账本
# ------------------------------------------------------------------------------
# 按**任务号**分桶：运行时改写本就随任务结束失效，隔离靠的就是这个键 —— 任务号由框架
# 的全局计数器发放（2000xxxxx 一路递增，跨 tasker 也不复用），别的任务的桶根本查不到，
# 不会把陈旧的还原点灌进一个没被改过的节点。
#
# ⚠️ 键里**不能**掺任何指针/地址。tasker 在这里没有可用的稳定身份：
#   - id(context.tasker)：binding 每次回调都新建 Context，Context.__init__ 又新建一个
#     Tasker 包装对象（maa/context.py::_init_tasker），拿到的是短命包装的地址；
#   - Tasker._handle：agent 模式下这是 MaaAgentServer 每次 MaaContextGetTasker 现分配的
#     **本地代理**地址，同样一次一变（宿主日志里那个恒定的 tasker_id 是宿主进程的指针，
#     不是 agent 侧的 _handle，别拿它当证据）。
# 两者都是小对象，地址会被回收复用 —— 于是绝大多数时候账本查不到、偶尔又撞上一个旧桶
# 还原出中间态，症状飘忽极难查。实测一次运行里同一任务连续拿到
# 9880 / 9c90 / 9f60 / a190 / 9f60 / 9880 / 95b0 七个不同的键。
#
# 多账号并行不需要在键里区分 tasker：每个实例是独立的 agent 进程，_LEDGERS 天然隔离。
#
# 注意 _LEDGERS 是 agent 进程里的普通全局 dict，框架不知道它的存在、也不会替我们清理：
# 框架只丢弃它自己那一半（任务结束时撤销 override）。所以任务一结束，对应的桶就变成了
# 谁也查不到的死数据，得靠 _ledger() 里那次清理回收，否则一直留到进程退出。
_LEDGERS: "dict[int, dict[str, dict]]" = {}

_MISSING = object()

# 框架对这两个字段是整体替换，不做合并 —— 它不理解里面的结构，所以不敢合并。
# 因此求差时**绝不能递归进去**：只改了其中一个键，也必须把整份旧值记下来，
# 否则还原时会把没改过的兄弟键一起抹掉。
_ATOMIC_KEYS = frozenset({"custom_action_param", "custom_recognition_param"})


def _ledger(argv: CustomAction.RunArg) -> dict:
    task_id = argv.task_detail.task_id
    # 只清任务号**比自己大**的桶。task_id 单调递增，所以"比我大"必然是我（或我的兄弟）
    # 派出去、且已经跑完的子任务 —— context.run_task() 每调一次就开一个新任务号。
    # 反过来写成 != 会让子任务把父任务的桶删掉：父任务只是在等子任务返回，并没有结束，
    # 它登记的还原点还要用。代价是先后两个顶层任务之间不再即时回收，旧桶留到 agent
    # 进程退出 —— 只有调过本模块的任务才建桶，量级可忽略。
    for stale in [k for k in _LEDGERS if k > task_id]:
        del _LEDGERS[stale]
    return _LEDGERS.setdefault(task_id, {})


def _diff(before, after, prefix=()):
    """列出 before -> after 变化的叶子路径，返回 [(路径, 旧值)]。

    字典递归下去，其余类型（含数组）整体比较 —— 框架对数组就是整体替换。
    原子字段（见 _ATOMIC_KEYS）同样整体比较，不递归。
    """
    out = []
    atomic = bool(prefix) and prefix[-1] in _ATOMIC_KEYS
    if not atomic and isinstance(before, dict) and isinstance(after, dict):
        for key in set(before) | set(after):
            out += _diff(before.get(key, _MISSING), after.get(key, _MISSING), prefix + (key,))
    elif before is not _MISSING or after is not _MISSING:
        if before != after:
            out.append((prefix, before))
    return out


def _build_restore(before: dict, after: dict):
    """把 diff 结果反推成最小还原载荷。返回 (载荷, 被跳过的新增路径)。"""
    payload: dict = {}
    skipped = []
    for path, old in _diff(before, after):
        if old is _MISSING:
            # 补丁新增了原本不存在的字段 —— 没有"原状"可回退，交给任务结束时自动失效
            skipped.append(".".join(str(p) for p in path))
            continue
        cursor = payload
        for seg in path[:-1]:
            cursor = cursor.setdefault(seg, {})
        cursor[path[-1]] = old
    return payload, skipped


# ------------------------------------------------------------------------------
# 参数与占位符
# ------------------------------------------------------------------------------

def _parse_param(argv: CustomAction.RunArg) -> dict:
    raw = argv.custom_action_param
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw).strip())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"custom_action_param 不是合法 JSON：{exc}") from None
    if not isinstance(parsed, dict):
        raise ConfigError(f"custom_action_param 必须是对象，实际是 {type(parsed).__name__}")
    return parsed


def _box_of(argv: CustomAction.RunArg):
    box = getattr(argv, "box", None)
    if not box or int(getattr(box, "w", 0)) <= 0 or int(getattr(box, "h", 0)) <= 0:
        return None
    return [int(box.x), int(box.y), int(box.w), int(box.h)]


def _resolve(data, node: str, caller: str, box):
    """把 patch 里的占位符替换掉。"""
    if isinstance(data, str):
        if data == "$box":
            if box is None:
                raise ConfigError(f"节点 [{node}] 的补丁用了 $box，但当前没有有效识别框")
            return list(box)
        if "$caller" in data and not caller:
            raise ConfigError(f"节点 [{node}] 的补丁用了 $caller，但参数里没写 caller")
        out = data.replace("$self", node)
        return out.replace("$caller", caller) if caller else out
    if isinstance(data, list):
        return [_resolve(x, node, caller, box) for x in data]
    if isinstance(data, dict):
        return {k: _resolve(v, node, caller, box) for k, v in data.items()}
    return data


ANCHOR_PREFIX = "[Anchor]"


def _resolve_anchor(context: Context, name: str) -> "str | None":
    """把 "[Anchor]锚点名" 解析成锚点当前指向的真实节点名；非该形式原样返回。

    配合上游节点的 anchor 字段自登记，让「共享结束节点」能精准操作「刚才是从哪个
    节点跳进来的」，即从哪进来就改哪个。

    锚点没指向任何节点时返回 None，调用方据此整个跳过这个目标 —— 补丁没有落点时
    什么都不做，好过拿字面量 "[Anchor]xxx" 去当节点名，那必然报节点不存在。
    """
    if not name.startswith(ANCHOR_PREFIX):
        return name

    anchor = name[len(ANCHOR_PREFIX):].strip()
    if not anchor:
        raise ConfigError(f"target {name!r} 带了 [Anchor] 前缀，但锚点名是空的")

    resolved = context.get_anchor(anchor)
    if not resolved:
        utils.mfaalog.warning(f"[Py] 🔗 锚点 [{anchor}] 未指向任何节点，跳过该目标")
        return None

    utils.mfaalog.info(f"[Py] 🔗 [Anchor]{anchor} → [{resolved}]")
    return resolved


def _resolve_targets(context: Context, spec, *, allow_regex: bool) -> "list[tuple[str, bool]]":
    """解析目标选择器 → [(节点名, 是否由锚点解析而来), ...]。

    三种写法**按序判定一次、选中即定**，不能既要还要：
      ① 固定节点名     "Node_A" / ["Node_A", "Node_B"]
      ② 锚点           "[Anchor]锚点名"（可出现在数组元素里；不支持正则）
      ③ 节点名正则     {"regex": "^Shop_.*$"}
    锚点是「运行时才知道是谁」的单个名字，正则是「在已加载节点名里搜一批」，两者语义
    互斥 —— 锚点名里写正则不会被当正则解释，只会拿去当锚点名查，查不到即跳过。

    第二个返回值标记来源，供上游禁止「锚点目标 + create 新建节点」。

    空字符串**不在这里吞掉**：它是配置错误（写了 target 却没写值），原样放行到下游
    的「节点不存在」检查去报错。只有锚点未命中（_resolve_anchor 返回 None）才过滤，
    那是流程的正常状态。两者语义不同，别再用真值判断混为一谈。
    """
    if isinstance(spec, str):
        hit = _resolve_anchor(context, spec)
        return [] if hit is None else [(hit, spec.startswith(ANCHOR_PREFIX))]
    if isinstance(spec, list):
        if not spec:
            raise ConfigError("target 是空数组")
        out = []
        for x in spec:
            raw = str(x)
            hit = _resolve_anchor(context, raw)
            if hit is not None:
                out.append((hit, raw.startswith(ANCHOR_PREFIX)))
        return out
    if isinstance(spec, dict) and "regex" in spec:
        if not allow_regex:
            raise ConfigError("RestorePipeline 的 target 不支持 regex，请写节点名或 \"*\"")
        raw = spec["regex"]
        patterns = [raw] if isinstance(raw, str) else list(raw)
        try:
            compiled = [re.compile(p) for p in patterns]
        except re.error as exc:
            raise ConfigError(f"target.regex 不是合法正则：{exc}") from None
        hit = [n for n in context.tasker.resource.node_list if any(c.search(n) for c in compiled)]
        if not hit:
            raise ConfigError(f"target.regex {patterns} 没有匹配到任何节点")
        return [(n, False) for n in hit]
    raise ConfigError(f"target 必须是节点名 / 数组 / {{\"regex\": ...}}，实际是 {spec!r}")


def _targets(context: Context, spec, *, allow_regex: bool) -> "list[str]":
    """只取节点名。RestorePipeline 用 —— 它按账本还原，不关心目标是怎么解析出来的。"""
    return [n for n, _ in _resolve_targets(context, spec, allow_regex=allow_regex)]


# ------------------------------------------------------------------------------
# 旁作用
# ------------------------------------------------------------------------------

def _side_reset_tags(params: dict) -> None:
    raw = params.get("reset_tags")
    if not raw:
        return
    tags = raw if isinstance(raw, list) else [raw]
    for tag in tags:
        TAG_STORE[tag] = 0
    utils.mfaalog.info(f"[Py] 🧹 [旁作用] 计数器已清零: {list(tags)}")


def _side_click(context: Context, argv: CustomAction.RunArg, spec) -> None:
    if spec is None:
        return
    box = _box_of(argv)
    if box is None:
        raise ConfigError("声明了 click 旁作用，但当前没有有效识别框")
    x, y, w, h = box
    offset = (spec or {}).get("offset") if isinstance(spec, dict) else None
    if offset:
        if not isinstance(offset, list) or len(offset) != 4:
            raise ConfigError(f"click.offset 必须是 4 元数组 [dx, dy, w, h]，实际是 {offset!r}")
        dx, dy, ow, oh = (int(v) for v in offset)
        cx, cy = x + dx + ow / 2, y + dy + oh / 2
    else:
        cx, cy = x + w / 2, y + h / 2
    context.tasker.controller.post_click(int(cx), int(cy))
    utils.mfaalog.info(f"[Py] 🖱️ [旁作用] 点击 ({int(cx)}, {int(cy)})")


# ------------------------------------------------------------------------------
# PatchPipeline
# ------------------------------------------------------------------------------

@AgentServer.custom_action("PatchPipeline")
class PatchPipeline(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _parse_param(argv)
            _side_reset_tags(params)

            has_pairs = "patches" in params
            has_single = "target" in params or "patch" in params
            if has_pairs and has_single:
                raise ConfigError("target/patch 与 patches 不能同时出现，请二选一")

            if has_pairs:
                pairs = params["patches"]
                if not isinstance(pairs, dict) or not pairs:
                    raise ConfigError("patches 必须是非空的 {节点名: 补丁} 对象")
                # 键同样过锚点解析 —— 与 target 写法保持一致的能力，别让两种写法一个
                # 认 "[Anchor]xxx" 一个不认（不解析的话，配上 create 会静默新建一个
                # 名字真就叫 "[Anchor]xxx" 的垃圾节点，补丁全打空）。
                plan = []
                for raw_name, sub in pairs.items():
                    raw_name = str(raw_name)
                    hit = _resolve_anchor(context, raw_name)
                    if hit is None:
                        continue        # 锚点没指向任何节点 → 跳过该目标（已在解析处告警）
                    plan.append((hit, sub, raw_name.startswith(ANCHOR_PREFIX)))
            else:
                if "target" not in params:
                    raise ConfigError("缺少 target（或改用 patches）")
                if "patch" not in params:
                    raise ConfigError("缺少 patch（或改用 patches）")
                patch = params["patch"]
                if not isinstance(patch, dict):
                    raise ConfigError(f"patch 必须是对象，实际是 {type(patch).__name__}")
                plan = [(n, patch, from_anchor) for n, from_anchor
                        in _resolve_targets(context, params["target"], allow_regex=True)]

            backup = params.get("backup", True)
            create = bool(params.get("create", False))
            caller = params.get("caller", "")
            box = _box_of(argv)
            ledger = _ledger(argv)

            patched, created, recorded = [], [], 0
            for node, raw_patch, from_anchor in plan:
                before = context.get_node_data(node)
                if before is None:
                    # 锚点目标一律不许新建：锚点指向的必然是流程里跑过的既有节点，解析
                    # 出来却查无此节点 = 上游 anchor 登记写错了。此时 create 会凭空造一个
                    # 同名空节点把错误盖住，补丁打在谁也不引用的节点上，返回还是成功。
                    if from_anchor:
                        raise ConfigError(
                            f"锚点解析到的节点 [{node}] 不存在。锚点只能指向已有节点，"
                            f"不允许借 create 新建 —— 请检查上游 anchor 字段登记的节点名"
                        )
                    if not create:
                        raise ConfigError(
                            f"节点 [{node}] 不存在（名字拼错？若确实想新建，请加 \"create\": true）"
                        )

                payload = _resolve(copy.deepcopy(raw_patch), node, caller, box)
                if not context.override_pipeline({node: payload}):
                    raise ConfigError(f"节点 [{node}] 的补丁被框架拒绝，请检查字段名与取值")

                if before is None:
                    created.append(node)
                    continue
                patched.append(node)

                # 先到先得：还原点永远是第一次打补丁之前的状态
                if backup and node not in ledger:
                    after = context.get_node_data(node) or {}
                    restore, skipped = _build_restore(before, after)
                    if restore:
                        ledger[node] = restore
                        recorded += 1
                    if skipped:
                        utils.mfaalog.info(
                            f"[Py] ℹ️ [{node}] 新增字段无原状可还原，随任务结束自动失效: {skipped}"
                        )

            if patched:
                utils.mfaalog.info(
                    f"[Py] 🔧 已改写 {len(patched)} 个节点（登记还原点 {recorded} 个）: "
                    f"{patched if len(patched) <= 6 else patched[:6] + ['...']}"
                )
            if created:
                utils.mfaalog.info(f"[Py] ✨ 新建节点 {len(created)} 个（不进账本）: {created}")

            _side_click(context, argv, params.get("click"))
            return True

        except ConfigError as exc:
            utils.mfaalog.error(f"[Py] ❌ PatchPipeline 配置错误: {exc}")
            return False
        except Exception as exc:
            import traceback
            utils.mfaalog.error(f"[Py] 💥 PatchPipeline 运行异常: {exc}")
            utils.mfaalog.error(traceback.format_exc())
            return False


# ------------------------------------------------------------------------------
# RestorePipeline
# ------------------------------------------------------------------------------

@AgentServer.custom_action("RestorePipeline")
class RestorePipeline(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _parse_param(argv)
            _side_reset_tags(params)

            if "target" not in params:
                raise ConfigError("缺少 target（节点名 / 数组 / \"*\"）")

            ledger = _ledger(argv)
            spec = params["target"]
            if spec == "*":
                targets = list(ledger.keys())
                if not targets:
                    utils.mfaalog.info("[Py] 🧹 本次任务没有需要还原的节点")
                    _side_click(context, argv, params.get("click"))
                    return True
            else:
                targets = _targets(context, spec, allow_regex=False)

            done, missing = [], []
            for node in targets:
                payload = ledger.get(node)
                if payload is None:
                    # 没被改过 / 已还原过 / 是新建的节点 —— 还原是幂等的，不算失败
                    missing.append(node)
                    continue
                if not context.override_pipeline({node: payload}):
                    raise ConfigError(f"节点 [{node}] 的还原被框架拒绝")
                # 销账必须在还原成功之后：先 pop 再失败会让还原点永久丢失，
                # 节点既停在被改写状态、又再也还原不回来（连 "*" 也查不到）
                ledger.pop(node, None)
                done.append(node)

            if done:
                utils.mfaalog.info(f"[Py] 🔙 已还原 {len(done)} 个节点: {done}")
            if missing:
                utils.mfaalog.warning(f"[Py] ⚠️ 账本中无还原点，已跳过（不算失败）: {missing}")

            _side_click(context, argv, params.get("click"))
            return True

        except ConfigError as exc:
            utils.mfaalog.error(f"[Py] ❌ RestorePipeline 配置错误: {exc}")
            return False
        except Exception as exc:
            import traceback
            utils.mfaalog.error(f"[Py] 💥 RestorePipeline 运行异常: {exc}")
            utils.mfaalog.error(traceback.format_exc())
            return False
