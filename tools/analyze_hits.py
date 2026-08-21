# -*- coding: utf-8 -*-
"""analyze_hits.py —— 从 maafw.log 统计节点真实命中次数, 用来校准 max_hit。

为什么需要它:
  给节点定 max_hit 时最难回答的是「这个节点在一次正常运行里合法命中几次」。
  靠读代码只能推断上界, 推错了两个方向都有代价: 给小了打断正常流程, 给大了止损慢。
  而这个数字其实一直躺在日志里 —— MaaFramework 对每次节点动作都发了结构化事件。

  于是校准不需要任何遥测: 你自己跑过的日志就是你自己的校准数据。

日志里的事实(实测 MaaFw 5.11):
  每次节点真正执行动作时, 日志有一行带 [msg=Node.Action.Succeeded] 与 JSON details,
  其中 name = 节点名, task_id = 本次任务运行的编号。max_hit 的语义正是「单个 task_id
  内累计命中数」, 与这里的口径完全一致, 所以按 (task_id, name) 计数即可。
  另有 Node.Recognition.* 系列(识别层, 命中数远大于动作层)与 Node.PipelineNode.*
  (节点层)。本工具默认用 Action.Succeeded —— 它对应「真的点下去了」, 是 max_hit 计数
  的那件事; 用 --event 可换成别的层次做对照。

用法:
  python tools/analyze_hits.py <maafw.log> [更多日志...]
      统计并打印每个节点的命中次数分布(按最大值排序)。
  python tools/analyze_hits.py <log> --check
      与当前 pipeline 里的 max_hit 对照, 标出风险:
        [紧] 实测最大命中 >= max_hit          —— 已经在触界边缘或已被截断
        [近] 实测最大命中 >= max_hit 的 70%   —— 余量偏薄
        [宽] max_hit >= 实测最大命中的 20 倍  —— 可以收紧, 缩短止损时间
  python tools/analyze_hits.py <log> --json out.json
"""
import argparse, io, json, os, re, sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 少于这么多轮的样本不做判定: 一两轮里混进一次失控, 中位数就失真了。
MIN_RUNS = 3

LINE_RE = re.compile(r"\[msg=(?P<msg>[A-Za-z.]+)\]\s+\[details=(?P<json>\{.*?\})\]\s*$")


def iter_events(path, want_msg):
    """逐行扫日志, 产出 (node_name, task_id)。日志可达几十 MB, 全程流式不进内存。

    去重是必须的: 实测同一个事件在日志里恰好出现两次(重复率 2.00x, 同毫秒、同 action_id),
    只按行数统计会让每个节点的命中数翻倍 —— 那会把 max_hit:1 的 Hub 门票节点算成命中 2 次,
    进而误判成"上限给小了"。details 里的 action_id/reco_id 是事件的唯一标识, 按它去重。
    """
    seen = set()
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if want_msg not in line:
                continue
            m = LINE_RE.search(line.rstrip("\n"))
            if not m or m.group("msg") != want_msg:
                continue
            try:
                d = json.loads(m.group("json"))
            except json.JSONDecodeError:
                continue
            name, tid = d.get("name"), d.get("task_id")
            if not name or tid is None:
                continue
            eid = d.get("action_id") or d.get("reco_id") or d.get("node_id")
            if eid is not None:
                key = (want_msg, eid)
                if key in seen:
                    continue
                seen.add(key)
            yield name, tid


def collect(paths, want_msg):
    """返回 {node: {task_id: 次数}}"""
    per = defaultdict(lambda: defaultdict(int))
    for p in paths:
        if not os.path.isfile(p):
            print(f"[warn] 跳过不存在的日志: {p}")
            continue
        n = 0
        for name, tid in iter_events(p, want_msg):
            per[name][tid] += 1
            n += 1
        print(f"[ok] {os.path.basename(p)}: {n} 条 {want_msg} 事件")
    return per


def load_max_hits():
    """读当前 pipeline 里已写的 max_hit。同名节点以 base 为准(平台包字段级继承)。"""
    out = {}
    for bundle in ("base", "pc", "playcover"):
        d = os.path.join(REPO, "assets", "resource", bundle, "pipeline")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            raw = io.open(os.path.join(d, fn), encoding="utf-8").read()
            try:
                doc = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))
            except json.JSONDecodeError:
                continue
            for name, node in doc.items():
                if isinstance(node, dict) and name not in out:
                    out[name] = node.get("max_hit")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--event", default="Node.Action.Succeeded",
                    help="统计哪一层事件(默认动作层, 与 max_hit 同口径)")
    ap.add_argument("--check", action="store_true", help="与当前 max_hit 对照标出风险")
    ap.add_argument("--json", default="")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-runs", type=int, default=MIN_RUNS, help="少于这么多轮不判定")
    a = ap.parse_args()
    globals()["MIN_RUNS"] = a.min_runs

    per = collect(a.logs, a.event)
    if not per:
        print("没有解析到任何事件 —— 确认日志里有该事件类型, 或换 --event")
        return 2

    rows = []
    for name, tasks in per.items():
        counts = sorted(tasks.values(), reverse=True)
        med = counts[len(counts) // 2]
        # p75 作为「正常运行的合法命中数」的代表, 而不是最大值。
        # 理由(实测): 日志里必然混着死循环的样本 —— Global_BackToQC_Gameplay_Anc 最大 708 次
        # 正是 08-14~17 那四天的 QC 页签死循环, 而它的中位只有 14。拿最大值当判据会把
        # 「界成功拦住了死循环」误报成「界给小了」, 结论正好反了。
        p75 = counts[max(0, int(len(counts) * 0.25))]
        rows.append({
            "node": name,
            "max": counts[0],
            "p75": p75,
            "median": med,
            "runs": len(counts),
            "total": sum(counts),
            # 最大值远高于中位 = 该节点历史上出现过失控轮次
            "runaway": counts[0] >= max(10, med * 8),
        })
    rows.sort(key=lambda r: -r["p75"])

    limits = load_max_hits() if a.check else {}
    print(f"\n节点数: {len(rows)}  任务运行数: {len(set(t for v in per.values() for t in v))}")
    print(f"\n{'节点':<50}{'p75':>5}{'中位':>5}{'最大':>7}{'轮数':>5}", end="")
    print(f"{'max_hit':>9}  判定" if a.check else "")
    print("-" * (72 + (20 if a.check else 0)))

    risky = {"紧": [], "近": [], "宽": []}
    # 判定跑全量, 只有打印受 --top 限制 —— 否则调小 top 会让汇总凭空少掉一批风险节点。
    for idx, r in enumerate(rows):
        show = idx < a.top
        line = f"{r['node']:<50}{r['p75']:>5}{r['median']:>5}{r['max']:>7}{r['runs']:>5}"
        if a.check:
            lim = limits.get(r["node"])
            tag = ""
            if lim and lim <= 2:
                # max_hit<=2 基本都是 Hub 门票模式(上游用 max_hit:1 当一次性门票, 执行完
                # 就退场让下一个 Hub 上场), 命中数等于上限是设计意图, 不是"被截断"。
                tag = "(门票模式, 不判定)"
            elif lim and r["runs"] < MIN_RUNS:
                tag = f"(样本{r['runs']}轮, 不足以判定)"
            elif lim:
                # 判据用中位数, 不用最大值也不用 p75 —— 日志里必然混着失控样本, 样本少时
                # p75 也会被它们顶起来(实测 PVP_PVPMes 的 p75=94 而中位只有 3)。
                # 中位代表「大多数运行的样子」, 才是正常合法命中数的代表。
                base = r["median"]
                if base >= lim:
                    tag = "[紧] 正常轮次已触界"
                    risky["紧"].append((r["node"], base, r["max"], lim))
                elif base >= lim * 0.7:
                    tag = "[近] 余量偏薄"
                    risky["近"].append((r["node"], base, r["max"], lim))
                elif lim >= base * 20 and lim >= 20:
                    tag = "[宽] 可收紧"
                    risky["宽"].append((r["node"], base, r["max"], lim))
                if r["runaway"]:
                    tag += " ⚡曾失控" + (f"(峰值{r['max']}, 界已生效)" if lim else "(无界!)")
            elif r["runaway"]:
                tag = f"⚡曾失控(峰值{r['max']})且无 max_hit"
            line += f"{lim if lim else '-':>9}  {tag}"
        if show:
            print(line)

    if a.check:
        print("\n== 汇总 ==")
        for k, label in (("紧", "正常轮次已触界(必须调大)"), ("近", "余量偏薄(建议调大)"),
                         ("宽", "余量过宽(可收紧以缩短止损)")):
            v = risky[k]
            print(f"  [{k}] {label}: {len(v)} 个")
            for node, base, mx, lim in v[:12]:
                print(f"       {node}: 中位={base} 峰值={mx}, max_hit={lim}")
        # 曾经失控 + 至今无界 = 最危险的组合: 有前科, 还没上锁
        hot = [r for r in rows if limits.get(r["node"]) is None and r["runaway"]]
        if hot:
            print(f"  [⚡] 有失控前科且仍无 max_hit: {len(hot)} 个")
            for r in hot[:12]:
                print(f"       {r['node']}: 峰值 {r['max']} 次(中位 {r['median']})")

    if a.json:
        io.open(a.json, "w", encoding="utf-8").write(
            json.dumps({"event": a.event, "rows": rows}, ensure_ascii=False, indent=2))
        print(f"\n完整数据已写入 {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
