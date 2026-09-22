"""NotRecognition —— 把「某条件没识别到」做成可组合的子识别。

`inverse` 是节点级参数，And / Or 的子项各自无法取反，于是「A 命中 且 B 未命中」
这类条件在 Pipeline 里写不出来。本识别器调用目标节点的识别部分并反转其结果，
让「非」成为 all_of / any_of 里的一个普通子项。

用法（V1 扁平）：

    {
        "Demo_CanOperate": {
            "recognition": "And",
            "all_of": [
                "Rec_Demo_Page",
                {
                    "recognition": "Custom",
                    "custom_recognition": "NotRecognition",
                    "custom_recognition_param": {"node": "Rec_Demo_Loading"}
                }
            ],
            "box_index": 0
        }
    }

四条容易踩反的约定，改之前先读：

1. 取反的是目标节点的**原始**识别，调用时强制 override `inverse: false`。
   `context.run_recognition` 底下走 TaskBase::run_recognition，会应用目标节点自己的
   `inverse`；而原生 And / Or 引用子节点时只取它的 reco_param，**不应用** inverse。
   不 override 的话，同一个节点被原生 And 引用、被这里引用，语义会不一样。
2. 全程只用 argv.image 这一张图，不重新截图、不等待、不重试。否则 And 里的几个条件
   会对应不同时刻的画面。
3. 通过 ≠ 找到了可点击的位置。命中时返回的框是 (0,0,0,0)，与框架 inverse 命中时给出的
   空 Rect 一致，只表示逻辑成立。后续动作要取坐标，得靠 And 的 box_index 指到别的子项。
4. 本节点自身的 roi 不参与取反，目标节点用的是它自己的 roi。所以别在 Not 这一侧写 roi——
   写了不起作用，写成「引用某前序节点」还会让框架在调到 Python 之前就判定不命中。

出错一律按「不通过」收场，同时记 error 日志并在 detail 里带 reason ——
不把故障折成一句「没有」。
"""

import json
import threading

from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition

from utils import mfaalog as logger

# 命中时返回的框：与框架 inverse 命中时给出的空 Rect 对齐，只表示「逻辑成立」。
HIT_BOX = (0, 0, 0, 0)

# 循环引用防线。主检测是下面按 task_id 分桶的调用栈；MAX_DEPTH 是兜底——
# 万一嵌套回调换了线程、task_id 取不到，靠它把无限递归截住。
MAX_DEPTH = 16

# key 只用框架发放的 task_id。嵌套调用是同步的，同一 task 内串行，
# 不同 task 各自分桶；锁只保护 dict 本身的增删。
_STACKS = {}
_STACKS_LOCK = threading.Lock()


def _push(task_id, node):
    """把 node 压进该 task 的调用栈。返回 (是否放行, 拒绝原因描述)。"""
    with _STACKS_LOCK:
        bucket = _STACKS.setdefault(task_id, {"stack": [], "tripped": False})
        stack = bucket["stack"]
        if node in stack:
            bucket["tripped"] = True
            return False, "循环引用: " + " -> ".join(stack + [node])
        if len(stack) >= MAX_DEPTH:
            bucket["tripped"] = True
            return False, f"嵌套深度超过 {MAX_DEPTH}: " + " -> ".join(stack[-4:] + [node])
        stack.append(node)
        return True, ""


def _pop(task_id):
    """出栈，并回报本条调用链上是否出现过循环 / 超深。

    tripped 必须往上传：被拒的那层返回「不通过」，它的上一层却只看得到一个
    hit=False，取反后会变成「通过」——配置错误就这么以成功收场了。
    """
    with _STACKS_LOCK:
        bucket = _STACKS.get(task_id)
        if not bucket:
            return False
        if bucket["stack"]:
            bucket["stack"].pop()
        tripped = bucket["tripped"]
        if not bucket["stack"]:
            _STACKS.pop(task_id, None)
        return tripped


def _miss(reason, **extra):
    """不通过（含各种出错）。reason 必填，保证识别记录里能分辨是真没命中还是出了故障。"""
    detail = {"result": "miss", "reason": reason}
    detail.update(extra)
    return CustomRecognition.AnalyzeResult(box=None, detail=detail)


@AgentServer.custom_recognition("NotRecognition")
class NotRecognition(CustomRecognition):
    def analyze(self, context, argv):
        here = getattr(argv, "node_name", "?")
        try:
            raw = getattr(argv, "custom_recognition_param", None)
            try:
                params = raw if isinstance(raw, dict) else json.loads(str(raw or "").strip() or "{}")
            except (ValueError, TypeError) as e:
                logger.error(f"NotRecognition[{here}] 参数不是合法 JSON: {raw!r} ({e})")
                return _miss("bad_param", param=repr(raw))

            node = params.get("node") if isinstance(params, dict) else None
            if isinstance(node, list):
                logger.error(f"NotRecognition[{here}] node 只接受单个节点名；多个条件请用 And / Or 组合: {node!r}")
                return _miss("bad_param", param=repr(node))
            if not isinstance(node, str) or not node:
                logger.error(f"NotRecognition[{here}] 缺少 node 参数: {params!r}")
                return _miss("bad_param", param=repr(raw))

            image = getattr(argv, "image", None)
            if image is None or getattr(image, "size", 0) == 0:
                logger.error(f"NotRecognition[{here}] 传入的截图为空，无法对 {node} 取反")
                return _miss("no_image", target=node)

            # 目标不存在时必须在这里拦住：下面 run_recognition 带着 override 调用，
            # 若节点原本不存在，那份 override 反而会凭空定义出一个节点，
            # 它没有 recognition 字段 → 按 DirectHit 恒命中 → 取反后恒不通过，且一声不吭。
            if context.get_node_data(node) is None:
                logger.error(f"NotRecognition[{here}] 目标节点不存在: {node}")
                return _miss("node_not_found", target=node)

            task_id = getattr(getattr(argv, "task_detail", None), "task_id", 0)
            ok, why = _push(task_id, node)
            if not ok:
                logger.error(f"NotRecognition[{here}] {why}")
                return _miss("recursion", target=node, chain=why)

            tripped = False
            try:
                reco = context.run_recognition(node, image, {node: {"inverse": False}})
            finally:
                tripped = _pop(task_id)

            if tripped:
                logger.error(f"NotRecognition[{here}] 调用链里出现过循环引用或超深嵌套，结果不可信: {node}")
                return _miss("recursion", target=node)

            # None = 识别没跑起来（节点被禁用、图像不合法等），与「跑了但没命中」不是一回事，
            # 不能当成「没有」进而取反为通过。
            if reco is None:
                logger.error(f"NotRecognition[{here}] 目标识别未能启动（节点被禁用？）: {node}")
                return _miss("reco_not_started", target=node)

            hit = getattr(reco, "hit", None)
            if not isinstance(hit, bool):
                logger.error(f"NotRecognition[{here}] 读不到 {node} 的 hit 字段: {reco!r}")
                return _miss("bad_reco_detail", target=node)

            info = {
                "target": node,
                "target_reco_id": getattr(reco, "reco_id", None),
                "target_algorithm": str(getattr(reco, "algorithm", "")),
            }
            if hit:
                logger.debug(f"NotRecognition[{here}] {node} 命中 → 不通过")
                return _miss("target_hit", **info)

            logger.debug(f"NotRecognition[{here}] {node} 未命中 → 通过")
            return CustomRecognition.AnalyzeResult(box=HIT_BOX, detail={"result": "hit", **info})

        except Exception as e:
            logger.error(f"NotRecognition[{here}] 异常: {e}")
            return _miss("exception", error=str(e))
