# -*- coding: utf-8 -*-
"""lint_pipeline.py —— MFABD2 资源静态体检。

为什么需要它: 上游现有的 check_resource.py 只回答"引擎能不能加载",
不回答"加载起来的东西对不对"。节点名 typo、模板图丢失、option 指向已删节点、
无界循环、ROI 越界——全部要等跑起来才炸, 而炸的形式往往是静默空转几十分钟。

七项检查(逐项独立, 可用 --only 单跑):
  1 parse      JSON/JSONC 可解析
  2 refs       next/on_error/interrupt/target/anchor 指向的节点存在
  3 templates  template 引用的图存在(双向: 丢引用 + 孤儿图)
  4 bounds     无界循环普查(自环/JumpBack 目标缺 max_hit) —— 与 bounded_safety_net 同口径
  5 roi        ROI/target 越界 1280x720 基准
  6 override   interface.json 的 pipeline_override 目标节点存在
  7 orphans    定义了但无人引用、且不是 entry 的节点(疑似死代码)

退出码: 0=全绿; 1=有 ERROR; 2=只有 WARN(可配 --strict 升级为 1)。
用法:
  python tools/lint_pipeline.py                      # 本仓库
  python tools/lint_pipeline.py --root E:/BrownDust2/MFABD2   # 部署实例
  python tools/lint_pipeline.py --only refs,bounds --json report.json
"""
import argparse, io, json, os, re, sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUMPBACK = "[JumpBack]"
# 虚拟/特殊引用前缀与保留名: 不当作真实节点名校验
VIRTUAL_PREFIX = ("[JumpBack]", "[Anchor]")
BASE_W, BASE_H = 1280, 720
CLICKY = {"Click", "LongPress", "Swipe", "MultiSwipe"}
CUSTOM_CLICKY = {"SmartSwipe"}

ALL_CHECKS = ["parse", "refs", "templates", "bounds", "roi", "override", "orphans"]


class Report:
    def __init__(self):
        self.items = []          # (level, check, where, msg)
        self._seen = set()

    def add(self, level, check, where, msg):
        # 去重: 同一节点会在多个资源组合(ADB/PC/PlayCover)里各被遍历一次,
        # 同名同问题只报一次, 否则计数会被组合数放大。
        k = (check, where, msg)
        if k in self._seen:
            return
        self._seen.add(k)
        self.items.append((level, check, where, msg))

    def errors(self):
        return [i for i in self.items if i[0] == "ERROR"]

    def warns(self):
        return [i for i in self.items if i[0] == "WARN"]


def resource_root(root):
    for base in (os.path.join(root, "assets", "resource"), os.path.join(root, "resource")):
        if os.path.isdir(base):
            return base
    raise SystemExit(f"找不到资源目录: {root}")


def interface_path(root):
    for p in (os.path.join(root, "assets", "interface.json"), os.path.join(root, "interface.json")):
        if os.path.isfile(p):
            return p
    return None


def strip_jsonc(raw):
    return re.sub(r"^\s*//.*$", "", raw, flags=re.M)


def load_json(path, rep):
    raw = io.open(path, encoding="utf-8", newline="").read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(strip_jsonc(raw))
        except json.JSONDecodeError as e:
            rep.add("ERROR", "parse", os.path.basename(path), f"JSON 解析失败: {e}")
            return None


def scan_bundles(res_root, rep):
    """返回 {bundle: {node_name: (path, node)}}, 以及合并后的全集(后加载覆盖先加载)。"""
    bundles = {}
    for b in sorted(os.listdir(res_root)):
        pdir = os.path.join(res_root, b, "pipeline")
        if not os.path.isdir(pdir):
            continue
        nodes = {}
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(pdir, fn)
            doc = load_json(p, rep)
            if doc is None:
                continue
            for name, node in doc.items():
                if isinstance(node, dict):
                    nodes[name] = (p, node)
        bundles[b] = nodes
    return bundles


def merged(bundles, order):
    out = {}
    for b in order:
        out.update(bundles.get(b, {}))
    return out


def flow_refs(node):
    """控制流引用: 决定"下一步跑哪个节点"。只有这些才构成循环。"""
    out = []
    for key in ("next", "on_error", "interrupt"):
        v = node.get(key)
        if isinstance(v, str):
            out.append((key, v))
        elif isinstance(v, list):
            out += [(key, x) for x in v if isinstance(x, str)]
    return out


def data_refs(node):
    """数据引用: target/begin/end 写节点名时表示"用那个节点识别到的位置",
    不是控制流跳转——把它算进自环判定会大面积误报(节点点自己识别到的框是常规写法)。"""
    out = []
    for key in ("target", "begin", "end"):
        v = node.get(key)
        if isinstance(v, str) and v not in ("true", "false"):
            out.append((key, v))
    return out


def node_refs(node):
    return flow_refs(node) + data_refs(node)


def deref(name):
    """剥掉 [JumpBack] 前缀得到真实节点名。

    注意两个不能碰的语法(2026-08-21 实测踩过):
      · [Anchor]X —— 运行时锚点, X 不是静态节点名, 静态期无从校验 → 由调用方跳过;
      · 名字里的 '#' —— 是节点名的组成部分(配合 PatchPipeline 占位符运行时替换成数字),
        不是 MAA 那种 '#next' 虚拟坐标。切掉它会让 Arbitrage_Card5#_Goin 这类真实节点查无此名。
    """
    if name.startswith(JUMPBACK):
        return name[len(JUMPBACK):]
    return name


def is_runtime_ref(name):
    """运行时才能解析的引用, 静态 lint 一律跳过。"""
    return name.startswith("[Anchor]") or name.startswith("$")


def action_type(node):
    a = node.get("action")
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return a.get("type")
    return None


def is_clicky(node):
    act = action_type(node)
    if act in CLICKY:
        return True
    return act == "Custom" and node.get("custom_action") in CUSTOM_CLICKY


# ── 检查项 ──────────────────────────────────────────────────────────────

def check_refs(nodes, rep):
    """引用完整性。self/back 之类的虚拟后缀先剥掉再判。"""
    known = set(nodes)
    for name, (path, node) in sorted(nodes.items()):
        for field, ref in node_refs(node):
            if is_runtime_ref(ref):
                continue
            t = deref(ref)
            if not t or t in known or is_runtime_ref(t):
                continue
            if field in ("target", "begin", "end") and t[0].isdigit():
                continue
            rep.add("ERROR", "refs", f"{os.path.basename(path)}::{name}",
                    f"{field} 指向不存在的节点: {ref}")


def check_templates(res_root, bundles, rep):
    """模板图双向对账: 引用了但文件不存在 / 存在但无人引用。"""
    for b, nodes in bundles.items():
        img_dir = os.path.join(res_root, b, "image")
        if not os.path.isdir(img_dir):
            continue
        on_disk = set()
        for dirpath, _d, files in os.walk(img_dir):
            for f in files:
                if f.lower().endswith((".png", ".jpg", ".bmp")):
                    rel = os.path.relpath(os.path.join(dirpath, f), img_dir).replace("\\", "/")
                    on_disk.add(rel)
        referenced = set()
        for name, (path, node) in nodes.items():
            tv = node.get("template")
            tl = [tv] if isinstance(tv, str) else (tv if isinstance(tv, list) else [])
            for t in tl:
                if not isinstance(t, str):
                    continue
                referenced.add(t.replace("\\", "/"))
                if t.replace("\\", "/") not in on_disk:
                    rep.add("ERROR", "templates", f"{os.path.basename(path)}::{name}",
                            f"模板图不存在: {t}")
        # 孤儿图只在 base 包报 WARN(pc 包本来就靠同名覆盖, 少量孤儿正常)
        if b == "base":
            for orphan in sorted(on_disk - referenced):
                rep.add("WARN", "templates", f"{b}/image", f"孤儿图(无人引用): {orphan}")


def check_bounds(nodes, rep):
    """无界循环: 自环或被 [JumpBack] 指向, 且无 max_hit。点击类=ERROR, 其余=WARN。"""
    jb = defaultdict(int)
    for name, (_p, node) in nodes.items():
        for _f, ref in flow_refs(node):
            if ref.startswith(JUMPBACK):
                jb[deref(ref)] += 1
    for name, (path, node) in sorted(nodes.items()):
        if "max_hit" in node:
            continue
        refs = [deref(r) for _f, r in flow_refs(node)]
        self_loop = name in refs
        jb_n = jb.get(name, 0)
        if not (self_loop or jb_n):
            continue
        why = []
        if self_loop:
            why.append("self-loop")
        if jb_n:
            why.append(f"JumpBack×{jb_n}")
        lvl = "ERROR" if is_clicky(node) else "WARN"
        extra = "" if "timeout" not in node else " (有 timeout, 但 timeout 只管'整轮无命中', 拦不住持续命中的自环)"
        rep.add(lvl, "bounds", f"{os.path.basename(path)}::{name}",
                f"无 max_hit 的{'点击类' if lvl=='ERROR' else '非点击类'}循环节点 [{'+'.join(why)}] action={action_type(node)}{extra}")


def check_roi(nodes, rep):
    """ROI/target 越界 1280x720。"""
    for name, (path, node) in sorted(nodes.items()):
        for key in ("roi", "target", "begin", "end"):
            v = node.get(key)
            if not (isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v)):
                continue
            x, y, w, h = v
            # [0,0,0,0] 是"全屏"的惯用写法
            if x == y == w == h == 0:
                continue
            if x + w > BASE_W or y + h > BASE_H or x < 0 or y < 0:
                rep.add("WARN", "roi", f"{os.path.basename(path)}::{name}",
                        f"{key}={v} 越出 {BASE_W}x{BASE_H} 基准")


def check_override(root, nodes, rep):
    """interface.json 的 pipeline_override / entry 指向的节点必须存在。"""
    ip = interface_path(root)
    if not ip:
        rep.add("WARN", "override", "interface.json", "未找到 interface.json, 跳过")
        return
    doc = load_json(ip, rep)
    if not doc:
        return
    known = set(nodes)

    def walk_override(ov, where):
        if not isinstance(ov, dict):
            return
        for node_name in ov:
            if node_name not in known:
                rep.add("ERROR", "override", where, f"pipeline_override 指向不存在的节点: {node_name}")

    for t in doc.get("task", []):
        nm = t.get("name", "?")
        for e in (t.get("entry"),):
            if e and e not in known:
                rep.add("ERROR", "override", f"task[{nm}]", f"entry 指向不存在的节点: {e}")
        walk_override(t.get("pipeline_override"), f"task[{nm}]")
    for opt_name, opt in (doc.get("option") or {}).items():
        for case in opt.get("cases", []):
            walk_override(case.get("pipeline_override"), f"option[{opt_name}/{case.get('name','?')}]")


def check_orphans(root, nodes, rep):
    """无人引用且非 entry 的节点(疑似死代码)。只报 WARN——上游可能留作手动调试入口。"""
    referenced = set()
    for name, (_p, node) in nodes.items():
        for _f, ref in node_refs(node):
            referenced.add(deref(ref))
    entries = set()
    ip = interface_path(root)
    if ip:
        doc = load_json(ip, rep)
        if doc:
            for t in doc.get("task", []):
                if t.get("entry"):
                    entries.add(t["entry"])
    for name, (path, node) in sorted(nodes.items()):
        if name in referenced or name in entries:
            continue
        if name.startswith("test_") or name.endswith("_Index"):
            continue
        rep.add("WARN", "orphans", f"{os.path.basename(path)}::{name}", "定义了但无人引用, 也不是 entry")


# ── 主流程 ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=REPO, help="仓库根或部署根")
    ap.add_argument("--only", default="", help=f"逗号分隔, 可选: {','.join(ALL_CHECKS)}")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    ap.add_argument("--json", default="", help="把完整结果写入 JSON 文件")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    a = ap.parse_args()

    checks = [c.strip() for c in a.only.split(",") if c.strip()] or ALL_CHECKS
    bad = [c for c in checks if c not in ALL_CHECKS]
    if bad:
        raise SystemExit(f"未知检查项: {bad}; 可选: {ALL_CHECKS}")

    rep = Report()
    res_root = resource_root(a.root)
    bundles = scan_bundles(res_root, rep)
    # 校验以"实际加载组合"为准: base 打底, 平台包覆盖。逐组合各查一遍。
    combos = [("ADB", ["base"])]
    for plat in ("pc", "playcover"):
        if plat in bundles:
            combos.append((plat.upper(), ["base", plat]))

    seen = set()
    for cname, order in combos:
        nodes = merged(bundles, order)
        for c in checks:
            key = (c, cname)
            if c in ("templates",) and cname != "ADB":
                continue  # 模板对账按 bundle 做一次即可
            if key in seen:
                continue
            seen.add(key)
            if c == "refs":
                check_refs(nodes, rep)
            elif c == "bounds":
                check_bounds(nodes, rep)
            elif c == "roi":
                check_roi(nodes, rep)
            elif c == "override":
                check_override(a.root, nodes, rep)
            elif c == "orphans":
                check_orphans(a.root, nodes, rep)
            elif c == "templates":
                check_templates(res_root, bundles, rep)

    # 输出
    by_check = defaultdict(lambda: {"ERROR": 0, "WARN": 0})
    for lvl, chk, _w, _m in rep.items:
        by_check[chk][lvl] += 1

    if not a.quiet:
        for chk in ALL_CHECKS:
            if chk not in checks:
                continue
            rows = [i for i in rep.items if i[1] == chk]
            if not rows:
                print(f"\n[{chk}] 通过")
                continue
            print(f"\n[{chk}] ERROR={by_check[chk]['ERROR']} WARN={by_check[chk]['WARN']}")
            for lvl, _c, where, msg in rows[:40]:
                print(f"  {lvl:5} {where}: {msg}")
            if len(rows) > 40:
                print(f"  ... 另有 {len(rows)-40} 条, 用 --json 导出完整清单")

    ne, nw = len(rep.errors()), len(rep.warns())
    print(f"\n{'='*60}")
    print(f"资源根: {res_root}")
    print(f"资源包: {', '.join(f'{b}({len(n)}节点)' for b, n in bundles.items())}")
    print(f"合计: ERROR={ne}  WARN={nw}")
    for chk in ALL_CHECKS:
        if chk in checks and (by_check[chk]["ERROR"] or by_check[chk]["WARN"]):
            print(f"  {chk:10} ERROR={by_check[chk]['ERROR']:<5} WARN={by_check[chk]['WARN']}")

    if a.json:
        io.open(a.json, "w", encoding="utf-8").write(json.dumps(
            [{"level": l, "check": c, "where": w, "msg": m} for l, c, w, m in rep.items],
            ensure_ascii=False, indent=2))
        print(f"完整清单已写入 {a.json}")

    if ne:
        return 1
    if nw and a.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
