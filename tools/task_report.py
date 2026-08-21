# -*- coding: utf-8 -*-
"""task_report.py —— 从 maafw.log 判读「每个任务到底做完没有」。

## 为什么需要它

MFABD2 现在无法回答一个基本问题: 昨晚那一轮, 哪些任务真的干完了?

原因在 `base/default_pipeline.json` 的一行全局默认: 所有没写 on_error 的节点,
失败后都落进 `Global_Null`(一个 DoNothing 空节点)然后链路"正常"结束。于是 GUI 上
任务显示完成、日志里 Tasker.Task.Succeeded 照发, 实际可能刚进门就夭折了。

所以本工具做两层判读, 而不是只看外层信号:

  第一层(外层): Tasker.Task.Starting / Succeeded 事件, details 里的 entry 直接
                对应 interface.json 的任务入口。缺 Succeeded = 确实没走完
                (崩溃/超时/被杀)。
  第二层(内层): 该任务期间到底执行了多少个节点动作、有多少节点失败。
                外层说成功、内层几乎没动作 = 空转, 这类才是最需要被看见的。

第二层存在的理由就是第一层不可信 —— 这与 MaaEnd/ok-ww/OneDragon 上反复踩到的
是同一个坑: 任何外层包装器的成功信号, 都必须再往里看一层。

## 用法

  python tools/task_report.py <maafw.log> [更多日志...]
  python tools/task_report.py <log> --json report.json
  python tools/task_report.py <log> --since 2026-08-20     # 只看某天起
"""
import argparse, io, json, os, re, sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
EVT_RE = re.compile(r"\[msg=(?P<msg>[A-Za-z.]+)\]\s+\[details=(?P<json>\{.*?\})\]\s*$")

WANT = {
    "Tasker.Task.Starting", "Tasker.Task.Succeeded", "Tasker.Task.Failed",
    "Node.Action.Succeeded", "Node.PipelineNode.Failed",
}
# 空转判据: 动作数少 **且** 耗时长。
#
# 只看动作数会误报天然短任务 —— 实测「[全局]结束游戏」就是 1 个动作 0 秒完事,
# 它本来就只需点一下关闭。真正的空转特征是「进去了、耗了时间、却没做事」:
# 要么一个动作都没有, 要么花了一分钟以上却只做了两三下。
SUSPICIOUS_ACTIONS = 3
SUSPICIOUS_SECONDS = 60


def entry_titles():
    """entry 名 -> 用户可见的任务名, 让报告说人话。"""
    out = {}
    for p in (os.path.join(REPO, "assets", "interface.json"),
              os.path.join(REPO, "interface.json")):
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            return out
        for t in d.get("task", []):
            if t.get("entry"):
                out[t["entry"]] = t.get("name", t["entry"])
        break
    return out


def parse(paths, since):
    """返回 {task_id: {...}}。一次流式扫描同时收集两层信息。"""
    tasks = {}
    seen_evt = set()
    for path in paths:
        # 键必须带文件名: task_id 在每次 MFABD2 启动时都从 200000001 重新编号,
        # 跨日志文件直接用 task_id 当键会把不同天的任务合并成一条, 统计全部失真。
        src = os.path.basename(path)
        if not os.path.isfile(path):
            print(f"[warn] 跳过不存在的日志: {path}")
            continue
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[msg=" not in line:
                    continue
                m = EVT_RE.search(line.rstrip("\n"))
                if not m or m.group("msg") not in WANT:
                    continue
                ts_m = TS_RE.match(line)
                ts = ts_m.group(1) if ts_m else ""
                if since and ts and ts[:10] < since:
                    continue
                try:
                    d = json.loads(m.group("json"))
                except json.JSONDecodeError:
                    continue
                tid = d.get("task_id")
                if tid is None:
                    continue
                msg = m.group("msg")

                # 同一事件在日志里恰好出现两次(实测重复率 2.00x), 按事件 id 去重;
                # Tasker 层没有 action_id, 用 (msg, task_id) 天然幂等。
                # 键同样必须带文件名: action_id 也是每次启动从 500000001 重新编号,
                # 跨文件共享去重集会把后一个文件的事件整批当成重复丢掉。
                eid = d.get("action_id") or d.get("reco_id")
                key = (src, msg, eid) if eid is not None else (src, msg, tid)
                if msg.startswith("Node.") and key in seen_evt:
                    continue
                seen_evt.add(key)

                t = tasks.setdefault((src, tid), {
                    "task_id": tid, "log": src, "entry": d.get("entry"), "start": None, "end": None,
                    "outcome": "未完成", "actions": 0, "failed_nodes": 0, "nodes": defaultdict(int),
                    # agent 侧 context.run_task() 也会新开 task_id, 但不发 Tasker.Task.*
                    # 事件。只有见过 Starting 的才是 GUI 队列里的真任务 —— 否则那些
                    # 逐件调用的子任务会被当成「没走完的任务」大批误报。
                    "is_real_task": False,
                })
                if d.get("entry") and not t["entry"]:
                    t["entry"] = d["entry"]
                if msg == "Tasker.Task.Starting":
                    t["start"] = t["start"] or ts
                    t["is_real_task"] = True
                elif msg == "Tasker.Task.Succeeded":
                    t["end"] = ts
                    t["outcome"] = "链路走完"
                elif msg == "Tasker.Task.Failed":
                    t["end"] = ts
                    t["outcome"] = "失败"
                elif msg == "Node.Action.Succeeded":
                    t["actions"] += 1
                    if d.get("name"):
                        t["nodes"][d["name"]] += 1
                    t["end"] = ts or t["end"]
                elif msg == "Node.PipelineNode.Failed":
                    t["failed_nodes"] += 1
    return tasks


def dur_sec(a, b):
    """两个时间戳之间的秒数; 无法计算时返回 None。"""
    if not (a and b):
        return None
    from datetime import datetime
    try:
        return (datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
                - datetime.strptime(a, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return None


def dur(a, b):
    if not (a and b):
        return ""
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        s = (datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds()
    except ValueError:
        return ""
    return f"{int(s)//60}分{int(s)%60:02d}秒"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--json", default="")
    ap.add_argument("--since", default="", help="只统计该日期(YYYY-MM-DD)起的记录")
    a = ap.parse_args()

    tasks = parse(a.logs, a.since)
    if not tasks:
        print("没有解析到任何任务事件")
        return 2
    titles = entry_titles()

    # MaaTaskerPostStop 之类是框架内部操作, 不是 GUI 队列里的任务
    NOT_TASK = {"MaaTaskerPostStop", "MaaTaskerPostTask"}
    rows = sorted((t for t in tasks.values()
                   if t["is_real_task"] and t.get("entry") not in NOT_TASK),
                  key=lambda t: (t["start"] or "", t["task_id"]))
    subtasks = [t for t in tasks.values() if not t["is_real_task"]]
    print(f"\n{'开始时间':<20}{'任务':<26}{'结果':<10}{'耗时':>9}{'动作':>6}{'失败节点':>8}  判读")
    print("-" * 108)

    suspicious, incomplete = [], []
    for t in rows:
        name = titles.get(t["entry"], t["entry"] or f"task_{t['task_id']}")
        verdict = ""
        if t["outcome"] == "未完成":
            verdict = "⚠ 没走完(崩溃/超时/被杀)"
            incomplete.append((name, t))
        elif t["actions"] == 0 or (t["actions"] < SUSPICIOUS_ACTIONS
                                    and (dur_sec(t["start"], t["end"]) or 0) >= SUSPICIOUS_SECONDS):
            # 这正是 default on_error -> Global_Null 把失败洗成成功的那一类
            verdict = f"⚠ 链路说成功但只做了 {t['actions']} 个动作 = 疑似空转"
            suspicious.append((name, t))
        elif t["failed_nodes"]:
            verdict = f"完成(途中 {t['failed_nodes']} 个节点失败)"
        else:
            verdict = "完成"
        print(f"{(t['start'] or '?'):<20}{name[:24]:<26}{t['outcome']:<10}"
              f"{dur(t['start'], t['end']):>9}{t['actions']:>6}{t['failed_nodes']:>8}  {verdict}")

    print(f"\n== 汇总: 共 {len(rows)} 次任务运行 ==")
    print(f"  没走完: {len(incomplete)} 次")
    for name, t in incomplete[:8]:
        print(f"     {name} (开始于 {t['start']}, 已做 {t['actions']} 个动作)")
    print(f"  链路说成功但疑似空转: {len(suspicious)} 次")
    for name, t in suspicious[:8]:
        top = sorted(t["nodes"].items(), key=lambda x: -x[1])[:3]
        print(f"     {name} (动作 {t['actions']} 个: {', '.join(n for n, _c in top) or '无'})")
    if not incomplete and not suspicious:
        print("  全部正常走完且有实际动作")

    if a.json:
        out = []
        for t in rows:
            o = {k: v for k, v in t.items() if k not in ("nodes", "is_real_task")}
            o["name"] = titles.get(t["entry"], t["entry"])
            o["duration"] = dur(t["start"], t["end"])
            o["top_nodes"] = sorted(t["nodes"].items(), key=lambda x: -x[1])[:10]
            out.append(o)
        io.open(a.json, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n完整数据已写入 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
