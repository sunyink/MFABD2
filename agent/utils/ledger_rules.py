"""库存记账的运行时判定：流程走到一个位置时，盘不盘、盘哪几类。

纯函数，不依赖框架、不读写存档。只问一句：上一次盘点之后，有没有还没盘掉的活动。
这里只决定盘点时机；段怎么划、算谁的、归哪天，是出报表时从凭证现算的事，不在运行时判。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

# 位置：begin 任务从这里开始动库存；end 正常结束点；queue_end 结束游戏；manual 手动结账。
MARKS = ("begin", "end", "queue_end", "manual")
# 每种类型自己要的扫描类型（背包仓检 Agt_BagScan_Config 里的键：A 存货、B 票券与养成材料、C 装备制作材料）。
NEEDS = {"daily": ("B", "C"), "collect": ("A",), "shop": ("A",)}
ALL_SCOPES = ("A", "B", "C")
GAP_SECONDS = 60.0


@dataclass
class LedgerState:
    """一个账号在本次 Agent 运行里的状态。只在内存里：进程一停就没了，下次自然按「不连续」处理。"""

    last_scan: dict | None = None                 # {"scan_id", "at", "scopes", "game_day", "task_id"}
    pending: list = field(default_factory=list)   # 上一次盘点之后的始标记：[{"kind", "task_id", "entry"}]
    seen: set = field(default_factory=set)        # 到过任何位置的任务号
    begun: set = field(default_factory=set)       # 留过始标记的 (任务号, 类型)


def game_day(now: float) -> str:
    """游戏日 09:00 KST 换日，正好是 UTC 日期。"""
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def _connected(rows, from_task, to_task, gap: float) -> bool:
    """从上一次盘点所在的任务到当前任务，任务一个接一个、中间没有超过阈值的空档。"""
    if from_task == to_task:
        return True
    previous_end, started = None, False
    for row in rows:
        if not started:
            if row["task_id"] == from_task:
                started, previous_end = True, row["end"]
            continue
        if previous_end is None or row["start"] - previous_end > gap:
            return False
        if row["task_id"] == to_task:
            return True
        previous_end = row["end"]
    return False


def _union(kinds) -> list:
    wanted = {scope for kind in kinds for scope in NEEDS[kind]}
    return [scope for scope in ALL_SCOPES if scope in wanted]


def decide(mark: str, kind: str | None, state: LedgerState, *, task_id: int, rows, now: float,
           quiet=(), gap: float = GAP_SECONDS) -> dict:
    """返回 {"scan", "scopes", "end_mark", "reason"}。

    rows：本次 Agent 运行里框架报过的任务，按开始时刻排，每行 {"entry", "task_id", "start", "end"}（秒；还在跑的 end 为 None）。
    quiet：没留标记也不算活动的任务入口——不动库存的，以及接了标记、没走到始标记就等于没干活的。
    end_mark：这里是不是该留正常结束标记（本任务留过同类型的始标记才留）。
    """
    if mark not in MARKS:
        raise ValueError(f"未知位置: {mark}")
    if (mark in ("begin", "end")) != (kind in NEEDS):
        raise ValueError(f"位置 {mark} 的类型不对: {kind}")
    last = state.last_scan
    connected = last is not None and _connected(rows, last["task_id"], task_id, gap)
    strangers = [] if last is None else [
        row["entry"] for row in rows
        if row["start"] > last["at"] and row["task_id"] != task_id
        and row["task_id"] not in state.seen and row["entry"] not in quiet]
    kinds = [item["kind"] for item in state.pending]
    end_mark = mark == "end" and (task_id, kind) in state.begun

    def result(scopes, reason):
        return {"scan": bool(scopes), "scopes": list(scopes), "end_mark": end_mark, "reason": reason}

    if mark == "begin" and kind is not None:
        if last is None:
            return result(ALL_SCOPES, "本次运行第一盘")
        if not connected:
            return result(ALL_SCOPES, "与上一次盘点不连续")
        if last["game_day"] != game_day(now):
            return result(ALL_SCOPES, "跨过游戏日")
        if strangers:
            return result(ALL_SCOPES, f"中间跑过没接记账的任务（{strangers[0]}）")
        if any(other != kind for other in kinds):
            return result(_union([kind, *kinds]), "类型变了，补上一阶段的期末")
        if kinds:
            return result((), "同类型接着做")
        missing = [scope for scope in NEEDS[kind] if scope not in last["scopes"]]
        if missing:
            return result(missing, "上一次盘点没盘到这一类要的")
        return result((), "上一次盘点一盘两用")

    if not kinds and not strangers and not (mark == "manual" and (last is None or not connected)):
        return result((), "没有还没盘掉的活动")
    if last is None or not connected or strangers:
        return result(ALL_SCOPES, "收尾，之前没有可接的盘点" if not strangers else f"收尾，中间跑过没接记账的任务（{strangers[0]}）")
    return result(_union(kinds), "收尾")


def settle(state: LedgerState, *, mark: str, kind: str | None, task_id: int, entry: str, now: float,
           scan: dict | None = None) -> None:
    """一个位置处理完之后更新状态。scan：盘点执行了就给 {"scan_id", "scopes"}；没盘、或盘点没执行，给 None。

    盘点没执行时未盘掉的活动原样留着，由下一个位置再判。
    """
    state.seen.add(task_id)
    if scan is not None:
        state.last_scan = {"scan_id": scan["scan_id"], "at": now, "scopes": frozenset(scan["scopes"]),
                           "game_day": game_day(now), "task_id": task_id}
        state.pending = []
    if mark == "begin":
        state.begun.add((task_id, kind))
        state.pending.append({"kind": kind, "task_id": task_id, "entry": entry})
