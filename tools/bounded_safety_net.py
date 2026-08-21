# -*- coding: utf-8 -*-
"""bounded_safety_net.py —— pipeline 无界循环普查与有界化(兜底安全网)工具。

背景(实测事故, 2026-08):
  MaaFW 的 timeout 只对「next 列表无命中」计时——识别一直命中就永不超时;
  max_hit 默认 UINT_MAX。于是「命中即点、点了画面不变」的节点就是永动机:
  - Collect_FindTeleporCircle0.9 (IcoHand 通用交互模板) 0.975 持续命中+点击+[JumpBack] 自环,
    2026-08-10 实测 914 次点击同一坐标; 08-20 变体「区域地图.tpN.找到传送阵图标」再烧 90 分钟。
  - Setup 滑动触底循环、QC 玩法页签循环(08-14~17 每天 80-90 分钟)同构。
  普查(2026-08-19): 1501 节点中 289 个自环、212 个既无 max_hit 也无 timeout;
  738 条 [JumpBack] 边指向 310 节点, 其中 160 个无 max_hit。

设计原则(参照上游已合并 PR #424「永动循环改为有界收场」, 避开被关闭的 #446 的教训):
  这是**不改变正常路径的兜底安全网**——正常流程一个节点命中几次就该推进;
  max_hit 达上限后该节点只是从 next 匹配中退场, 让后续节点/on_error 有机会接手,
  不改变任何"本来就能走通"的路径。

用法:
  python tools/bounded_safety_net.py --census
      全量普查, 输出风险节点清单(按文件分组)。
  python tools/bounded_safety_net.py --apply --files Collect_Navigation,Collect_TeleportRecall,Collect_skills,Setup [--value 10]
      对指定文件里的风险节点做定点文本插入 "max_hit": N (不重排全文, diff 最小);
      改后逐文件 JSON 校验, 校验不过则回滚该文件。
"""
import argparse, io, json, os, re, sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE_DIRS = [
    os.path.join(ROOT, "assets", "resource", "base", "pipeline"),
    os.path.join(ROOT, "assets", "resource", "pc", "pipeline"),
]


def set_root(root):
    # --root 指向部署目录(资源在 <root>/resource/)或仓库目录(资源在 <root>/assets/resource/)
    global PIPE_DIRS
    for base in (os.path.join(root, "assets", "resource"), os.path.join(root, "resource")):
        if os.path.isdir(base):
            PIPE_DIRS = [
                os.path.join(base, "base", "pipeline"),
                os.path.join(base, "pc", "pipeline"),
            ]
            return
    raise SystemExit(f"--root 下找不到资源目录: {root}")

CLICKY = {"Click", "LongPress", "Swipe", "MultiSwipe"}
# Custom 动作里已确认属于"点击/滑动同类"的白名单(不敢盲扩: 别的 custom 语义未知)。
# SmartSwipe: 08-18 实测 Setup_Main_Swip(Custom/SmartSwipe) 触底后被 [JumpBack] 拉回无限循环。
CUSTOM_CLICKY = {"SmartSwipe"}
JUMPBACK = "[JumpBack]"


def is_clicky(node):
    act = action_type(node)
    if act in CLICKY:
        return True
    if act == "Custom" and node.get("custom_action") in CUSTOM_CLICKY:
        return True
    return False


def load_jsonc(path):
    raw = io.open(path, encoding="utf-8", newline="").read()
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        # 宽容: 去整行 // 注释后再试(上游 check_resource.py 用 jsonc 库, 说明允许注释)
        stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
        return json.loads(stripped), raw


def action_type(node):
    a = node.get("action")
    if a is None:
        return None
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return a.get("type")
    return None


def next_list(node):
    out = []
    for key in ("next", "on_error", "interrupt"):
        v = node.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            out.append((key, v))
        elif isinstance(v, list):
            out += [(key, x) for x in v if isinstance(x, str)]
    return out


def strip_jb(name):
    return name[len(JUMPBACK):] if name.startswith(JUMPBACK) else name


def scan():
    """返回 files: {path: (doc, raw)}, nodes: {name: (path, node)}, jb_targets: {name: 引用次数}"""
    files, nodes, jb = {}, {}, defaultdict(int)
    for d in PIPE_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                doc, raw = load_jsonc(p)
            except Exception as e:
                print(f"[warn] 解析失败跳过 {fn}: {e}")
                continue
            files[p] = (doc, raw)
            for name, node in doc.items():
                if not isinstance(node, dict):
                    continue
                # 同名节点先到先得。PIPE_DIRS 里 base 排在平台包前面, 所以这里保留的是
                # base 版本 —— 兜底字段必须写进 base: ADB 组合只加载 base, 而平台包是
                # 字段级 merge, base 有了 max_hit 平台包会自动继承。若反过来只写平台包,
                # ADB 组合就漏保护(2026-08-21 实测 StartGame_7dAD_Check 正是这样漏的)。
                if name in nodes:
                    continue
                nodes[name] = (p, node)
                for _k, ref in next_list(node):
                    if ref.startswith(JUMPBACK):
                        jb[strip_jb(ref)] += 1
    return files, nodes, jb


def classify(nodes, jb):
    """风险分级。返回 [(name, path, reasons, act, has_timeout)]"""
    out = []
    for name, (path, node) in nodes.items():
        if "max_hit" in node:
            continue
        act = action_type(node)
        refs = [strip_jb(r) for _k, r in next_list(node)]
        self_loop = name in refs
        jb_target = jb.get(name, 0) > 0
        if not (self_loop or jb_target):
            continue
        reasons = []
        if self_loop:
            reasons.append("self-loop")
        if jb_target:
            reasons.append(f"JumpBack×{jb[name]}")
        out.append((name, path, reasons, act, "timeout" in node, is_clicky(node)))
    return out


def census():
    files, nodes, jb = scan()
    risky = classify(nodes, jb)
    clicky = [r for r in risky if r[5]]
    other = [r for r in risky if not r[5]]
    print(f"pipeline 文件: {len(files)}  节点: {len(nodes)}")
    print(f"无 max_hit 的自环/JumpBack 目标: {len(risky)}  其中点击类(建议有界化): {len(clicky)}  非点击类(逐个评估): {len(other)}")
    by_file = defaultdict(list)
    for name, path, reasons, act, has_to, _c in clicky:
        by_file[os.path.basename(path)].append((name, reasons, act, has_to))
    print("\n== 点击类风险节点(按文件) ==")
    for fn in sorted(by_file, key=lambda k: -len(by_file[k])):
        print(f"\n-- {fn} ({len(by_file[fn])}) --")
        for name, reasons, act, has_to in by_file[fn]:
            print(f"   {name}  [{'+'.join(reasons)}] action={act}{' timeout✓' if has_to else ''}")
    print("\n== 非点击类(参考, 本工具不动) ==")
    byf2 = defaultdict(int)
    for _n, path, *_ in other:
        byf2[os.path.basename(path)] += 1
    for fn, c in sorted(byf2.items(), key=lambda x: -x[1]):
        print(f"   {fn}: {c}")


# MapChange/Lift: 导航地图翻页(NviMapChange_Lift/Right, 上游把 Left 拼成 Lift),
# 实测一次正常运行连按 4 次左移, 宽地图更多 —— 属连续导航, 归宽档。
# RewardBubble: 逐个点掉奖励气泡, 合法命中数 = 气泡个数, 可能超过紧档。
REPETITIVE = re.compile(r"Swip|Swipe|Move|Scroll|MapChange|Lift|RewardBubble|左移|右移|上移|下移|滚动")

# 技能施放类: 合法次数由**账号天赋**决定, 不是流程决定的 —— 实测吸取 21 次/召集 14 次,
# 而且这类流程的设计出口正是"OCR 到 N/N 次数用尽"。按普通点击的紧档封顶会造成双重损失:
# 既拿不满次数, 又永远走不到设计出口, 于是在剩余卡带里空转。
# (2026-08-21 实测: 我们把 Collect_Skill3_2 封在 10, 而账号有 21 次。)
SKILL_LIKE = re.compile(r"Skill|技能")
SKILL_VALUE = 40


def bound_for(name, node, value, swipe_value):
    # 滑动/连续导航类节点: 合法重复次数天然高(实测导航地图连左移4次是正常路径),
    # 上限给宽(默认15), 只拦"永动", 不拦正常翻页。其余点击类给紧(默认5)。
    if SKILL_LIKE.search(name):
        return SKILL_VALUE
    act = action_type(node)
    if act in ("Swipe", "MultiSwipe"):
        return swipe_value
    if act == "Custom" and node.get("custom_action") in CUSTOM_CLICKY:
        return swipe_value
    if REPETITIVE.search(name):
        return swipe_value
    return value


def apply(files_filter, value, swipe_value, plan=None):
    """plan: {节点名: max_hit} —— 给定则按清单精确施工(值来自逐节点语义审查),
    不给则按 bound_for 的启发式分档。清单模式仍只动"确实无界"的节点:
    清单里已有 max_hit 的节点会被 classify 过滤掉, 避免重复插入。"""
    files, nodes, jb = scan()
    risky = classify(nodes, jb)
    if plan is not None:
        targets = [(name, path) for name, path, _r, _a, _t, _ck in risky if name in plan]
    else:
        targets = [
            (name, path) for name, path, _r, _act, _t, ck in risky
            if ck and os.path.basename(path).replace(".json", "") in files_filter
        ]
    by_path = defaultdict(list)
    for name, path in targets:
        by_path[path].append(name)

    total = 0
    for path, names in by_path.items():
        raw = files[path][1]
        text = raw
        done, missed = [], []
        for name in names:
            # 定点插入: 节点开括号行之后, 以内层缩进插入 "max_hit": N,
            pat = re.compile(r'^([ \t]*)("%s": \{)[ \t]*\r?$' % re.escape(name), re.M)
            m = pat.search(text)
            if not m:
                missed.append(name)
                continue
            indent = m.group(1) + "    "
            eol = "\r\n" if "\r\n" in text else "\n"
            nv = plan[name] if plan is not None else bound_for(name, nodes[name][1], value, swipe_value)
            insert = f"{indent}\"max_hit\": {nv},{eol}"
            pos = m.end()
            # m.end() 停在行尾(\r 之前或行末), 推进到下一行行首
            nl = text.find("\n", pos)
            text = text[: nl + 1] + insert + text[nl + 1:]
            done.append(name)
        if text != raw:
            try:
                json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
            except Exception as e:
                print(f"[abort] {os.path.basename(path)} 插入后 JSON 不合法, 回滚: {e}")
                continue
            io.open(path, "w", encoding="utf-8", newline="").write(text)
            total += len(done)
            print(f"[ok] {os.path.basename(path)}: 有界化 {len(done)} 个节点" + (f"; 未匹配 {missed}" if missed else ""))
        elif missed:
            print(f"[warn] {os.path.basename(path)}: 全部未匹配 {missed}")
    print(f"\n合计有界化 {total} 个节点 (点击={value}, 滑动/导航={swipe_value})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--files", default="", help="逗号分隔的文件名(不带 .json)")
    # 默认 10 而非 5: false-positive 的代价是打断正常流程, 网络卡顿时多点几下是常态;
    # 10 次 ≈ 10-30 秒即止损, 相比事故里 90 分钟的空转已是三个数量级的改善。
    ap.add_argument("--value", type=int, default=10)
    ap.add_argument("--swipe-value", type=int, default=15)
    ap.add_argument("--root", default="", help="资源根目录(默认: 本仓库)")
    ap.add_argument("--plan", default="", help='JSON 文件 {"节点名": max_hit}, 按清单精确施工')
    a = ap.parse_args()
    if a.root:
        set_root(a.root)
    if a.census:
        census()
    elif a.apply:
        plan = None
        if a.plan:
            plan = json.load(io.open(a.plan, encoding="utf-8"))
            print(f"按清单施工: {len(plan)} 个节点 ({a.plan})")
        elif not a.files:
            print("--apply 需要 --files 或 --plan"); return 2
        apply(set(a.files.split(",")) if a.files else set(), a.value, a.swipe_value, plan)
    else:
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main() or 0)
