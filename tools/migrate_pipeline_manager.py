# -*- coding: utf-8 -*-
"""
pipeline_manager 重构 —— pipeline JSON 调用点迁移脚本

把 pipe 侧的旧动作调用改写为新 API：

    PatchNode / PatchBatch / PatchByRegex / PatchAndClick  ->  PatchPipeline
    RestoreNode / RestoreBatch / ResetAll                  ->  RestorePipeline
    RunTask / Log                                          ->  删除动作三件套（节点保留）

同时删除全部手写的 origin / origins —— 新实现自动从运行时取还原点（设计文档 §5.2）。

【为什么是外科式文本替换而不是 json 重新序列化】
本仓库的 JSON 由 prettier + @nekosu/prettier-plugin-maafw-sort 格式化，而该 checkout 的
node_modules 是从别的机器带来的（.bin shim 指向不存在的 F:\\），prettier 跑不起来。
直接 json.dumps(indent=4) 重写会在受影响文件里引入 325 行纯格式噪音（真实改动只有 62 处），
把 diff 埋掉。故本脚本只替换目标节点的 custom_action / custom_action_param 两处值的字节区间，
其余字节原样保留。改完会做「语义校验」：重新解析并与预期结构逐节点比对，不一致即中止。

用法：
    python tools/migrate_pipeline_manager.py            # 预演，只出报告不写盘
    python tools/migrate_pipeline_manager.py --apply    # 真改
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIRS = [
    ROOT / "assets" / "resource" / "base" / "pipeline",
    ROOT / "assets" / "resource" / "pc" / "pipeline",
]

PATCH_ACTIONS = {"PatchNode", "PatchBatch", "PatchByRegex", "PatchAndClick"}
RESTORE_ACTIONS = {"RestoreNode", "RestoreBatch", "ResetAll"}
DROP_ACTIONS = {"RunTask", "Log"}
ACTION_KEYS = ("action", "custom_action", "custom_action_param")

DEC = json.JSONDecoder()
WS = " \t\r\n"

changes: list[dict[str, Any]] = []
warnings: list[str] = []


def warn(msg: str) -> None:
    warnings.append(msg)


# ============================================================================
# 带位置信息的 JSON 成员扫描（只走直接成员，不误入嵌套）
# ============================================================================

def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in WS:
        i += 1
    return i


def iter_members(s: str, brace: int) -> Iterator[tuple[str, int, int, int]]:
    """brace 指向 '{'。产出 (key, key_start, value_start, value_end)，仅直接成员。"""
    i = _skip_ws(s, brace + 1)
    if i >= len(s) or s[i] == "}":
        return
    while True:
        key_start = i
        key, i = DEC.raw_decode(s, i)
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] != ":":
            raise ValueError(f"位置 {i} 期望 ':'")
        i = _skip_ws(s, i + 1)
        value_start = i
        _, i = DEC.raw_decode(s, i)
        yield key, key_start, value_start, i
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i = _skip_ws(s, i + 1)
            continue
        break


def top_level_members(s: str) -> list[tuple[str, int, int, int]]:
    i = _skip_ws(s, 0)
    if i >= len(s) or s[i] != "{":
        raise ValueError("顶层不是对象")
    return list(iter_members(s, i))


def indent_of(s: str, pos: int) -> str:
    """pos 所在行的前导空白。"""
    ls = s.rfind("\n", 0, pos) + 1
    return s[ls:pos] if s[ls:pos].strip() == "" else ""


def render(value: Any, base_indent: str) -> str:
    """把值序列化成与周围缩进对齐的 JSON 文本（首行不带缩进，由原位置提供）。"""
    txt = json.dumps(value, ensure_ascii=False, indent=4)
    lines = txt.split("\n")
    return "\n".join([lines[0]] + [base_indent + ln for ln in lines[1:]])


# ============================================================================
# 参数改写
# ============================================================================

def convert_patch(action: str, p: dict, where: str) -> dict | None:
    out: OrderedDict[str, Any] = OrderedDict()

    if action == "PatchNode":
        node, patch = p.get("node"), p.get("patch")
        if not node or patch is None:
            warn(f"{where}: PatchNode 缺 node/patch，跳过")
            return None
        out["target"] = node
        out["patch"] = patch

    elif action == "PatchBatch":
        patches = p.get("patches")
        if not isinstance(patches, dict) or not patches:
            warn(f"{where}: PatchBatch 的 patches 为空或非字典，跳过")
            return None
        if "node" in p:
            warn(f"{where}: PatchBatch 带了多余的 node={p['node']!r}（旧实现从不读它），已丢弃")
        if len(patches) == 1:
            node, patch = next(iter(patches.items()))
            out["target"] = node
            out["patch"] = patch
        else:
            out["patches"] = patches

    elif action == "PatchByRegex":
        rules = p.get("rules")
        if rules is None:
            rules = [p]
        if not isinstance(rules, list) or len(rules) != 1:
            n = len(rules) if isinstance(rules, list) else "?"
            warn(f"{where}: PatchByRegex 有 {n} 条 rule，新 API 一个动作只支持一个选择器，需人工拆分 —— 跳过")
            return None
        rule = rules[0]
        pattern = rule.get("pattern")
        if not pattern:
            warn(f"{where}: PatchByRegex 缺 pattern，跳过")
            return None
        if rule.get("target_path") is not None:
            warn(f"{where}: 使用了已删除的 target_path（§6），需人工改写为全量数组 —— 跳过")
            return None
        patch = rule.get("patch")
        if patch is None:
            warn(f"{where}: PatchByRegex 缺 patch，跳过")
            return None
        for pat in pattern if isinstance(pattern, list) else [pattern]:
            if isinstance(pat, str) and "*" in pat and not any(c in pat for c in ".+[](){}\\"):
                warn(f"{where}: 正则 {pat!r} 疑似把 glob 当正则写"
                     f"（`*` 在正则里是「前一字符重复 0 次或多次」，不是通配），原样保留请复核")
        out["target"] = {"regex": pattern}
        out["patch"] = patch
        if p.get("caller"):
            out["caller"] = p["caller"]

    elif action == "PatchAndClick":
        node, patch = p.get("node"), p.get("patch")
        if node and patch is not None:
            out["target"] = node
            out["patch"] = patch
        offset = p.get("target_offset")
        out["click"] = {"offset": offset} if offset else {}

    if p.get("reset_tags"):
        out["reset_tags"] = p["reset_tags"]

    for k in ("origin", "origins"):
        if k in p:
            changes.append({"kind": "drop_origin", "where": where, "key": k})

    return dict(out)


def convert_restore(action: str, p: dict, where: str) -> dict | None:
    out: OrderedDict[str, Any] = OrderedDict()

    if action == "RestoreNode":
        node = p.get("node")
        if not node:
            warn(f"{where}: RestoreNode 缺 node，跳过")
            return None
        if p.get("backup") is not None:
            warn(f"{where}: RestoreNode 带了旧的 backup 参数（新实现由账本自动管理），已丢弃")
        out["target"] = node

    elif action == "RestoreBatch":
        nodes = p.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            warn(f"{where}: RestoreBatch 的 nodes 为空或非列表，跳过")
            return None
        out["target"] = nodes[0] if len(nodes) == 1 else nodes

    elif action == "ResetAll":
        out["target"] = "*"

    if p.get("reset_tags"):
        out["reset_tags"] = p["reset_tags"]

    return dict(out)


def locate_action_container(text: str, node_brace: int, cfg: dict) -> tuple[int, dict] | None:
    """定位 custom_action 所在的容器。

    本仓库 V1 扁平与 V2 嵌套两种写法混用：
        V1: {"custom_action": "X", "custom_action_param": {...}}          -> 容器是节点本身
        V2: {"action": {"type": "Custom", "param": {"custom_action": ...}}} -> 容器是 action.param
    返回 (容器的 '{' 位置, 容器 dict)。
    """
    if "custom_action" in cfg:
        return node_brace, cfg
    act = cfg.get("action")
    if isinstance(act, dict) and isinstance(act.get("param"), dict) and "custom_action" in act["param"]:
        for k, _ks, vs, _ve in iter_members(text, node_brace):
            if k != "action":
                continue
            for k2, _ks2, vs2, _ve2 in iter_members(text, vs):
                if k2 == "param":
                    return vs2, act["param"]
    return None


def plan_node(fname: str, node_name: str, cfg: dict) -> tuple[str, dict | None] | None:
    """返回 (新动作名, 新参数) ；删除类返回 ('', None)；不涉及返回 None。"""
    action = cfg.get("custom_action")
    if action not in PATCH_ACTIONS | RESTORE_ACTIONS | DROP_ACTIONS:
        return None

    where = f"{fname} / {node_name}"
    raw = cfg.get("custom_action_param")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            warn(f"{where}: custom_action_param 是字符串且解析失败（{e}），跳过")
            return None
    params = raw if isinstance(raw, dict) else {}

    if action in DROP_ACTIONS:
        remaining = [k for k in cfg if k not in ACTION_KEYS]
        note = "节点保留，仅摘掉 action 三件套（无 action 即 DoNothing）"
        if not remaining:
            note += "；⚠️ 摘完后该节点为空对象 {}"
        changes.append({"kind": "drop_action", "where": where, "old": action, "note": note})
        return ("", None)

    if action in PATCH_ACTIONS:
        new_action, new_params = "PatchPipeline", convert_patch(action, params, where)
    else:
        new_action, new_params = "RestorePipeline", convert_restore(action, params, where)
    if new_params is None:
        return None

    changes.append({
        "kind": "rewrite", "where": where, "old": action, "new": new_action,
        "old_params": params, "new_params": new_params,
    })
    return (new_action, new_params)


# ============================================================================
# 外科式写盘
# ============================================================================

def rewrite_text(text: str, fname: str) -> tuple[str, dict, bool]:
    """返回 (新文本, 期望结构, 是否有改动)。期望结构用于事后语义校验。"""
    data = json.loads(text, object_pairs_hook=OrderedDict)
    expect = copy.deepcopy(data)
    edits: list[tuple[int, int, str]] = []   # (start, end, 替换文本)
    touched = False

    for node_name, nstart, vstart, vend in top_level_members(text):
        cfg = data.get(node_name)
        if not isinstance(cfg, dict):
            continue
        loc = locate_action_container(text, vstart, cfg)
        if loc is None:
            continue
        container_brace, container = loc
        is_v2 = container is not cfg

        plan = plan_node(fname, node_name, container)
        if plan is None:
            continue
        touched = True
        new_action, new_params = plan

        # expect 的写入位置：V1 是节点本身，V2 是 action.param
        exp = expect[node_name]["action"]["param"] if is_v2 else expect[node_name]

        if is_v2 and new_params is None:
            # V2 的删除：整个 action 成员摘掉
            for k, ks, _vs, ve in iter_members(text, vstart):
                if k != "action":
                    continue
                ind = indent_of(text, ks)
                start = ks - len(ind) if ind else ks
                end = ve
                j = end
                while j < len(text) and text[j] in " \t":
                    j += 1
                if j < len(text) and text[j] == ",":
                    end = j + 1
                if end < len(text) and text[end] == "\n":
                    end += 1
                edits.append((start, end, ""))
                expect[node_name].pop("action", None)
                break
            continue

        mlist = list(iter_members(text, container_brace))
        members = {k: (ks, vs, ve) for k, ks, vs, ve in mlist}

        if new_params is None:
            # 删除三件套。逐成员精确定位，绝不事后扫文本找悬挂逗号
            # （那样会误伤字符串内的逗号，例如正则 `.{5,}$` —— 与 ISS #409 同类）
            drop_idx = [i for i, m in enumerate(mlist) if m[0] in ACTION_KEYS]
            for k in ACTION_KEYS:
                exp.pop(k, None)

            if len(drop_idx) == len(mlist):
                # 整个节点被清空 -> 直接写成 {}
                edits.append((vstart, vend, "{}"))
            else:
                runs: list[list[int]] = []
                for i in drop_idx:
                    if runs and runs[-1][-1] == i - 1:
                        runs[-1].append(i)
                    else:
                        runs.append([i])
                for run in runs:
                    first, last = run[0], run[-1]
                    fks = mlist[first][1]
                    lve = mlist[last][3]
                    if last == len(mlist) - 1:
                        # 删到末尾：往回吃掉前一个保留成员的逗号
                        prev_end = mlist[first - 1][3]
                        j = prev_end
                        while j < len(text) and text[j] in " \t":
                            j += 1
                        start = j if (j < len(text) and text[j] == ",") else prev_end
                        edits.append((start, lve, ""))
                    else:
                        ind = indent_of(text, fks)
                        start = fks - len(ind) if ind else fks
                        end = lve
                        j = end
                        while j < len(text) and text[j] in " \t":
                            j += 1
                        if j < len(text) and text[j] == ",":
                            end = j + 1
                        if end < len(text) and text[end] == "\n":
                            end += 1
                        edits.append((start, end, ""))
        else:
            ks, vs, ve = members["custom_action"]
            edits.append((vs, ve, json.dumps(new_action, ensure_ascii=False)))
            exp["custom_action"] = new_action

            if "custom_action_param" in members:
                ks2, vs2, ve2 = members["custom_action_param"]
                edits.append((vs2, ve2, render(new_params, indent_of(text, ks2))))
            else:
                # 原本没有 param 键（如 ResetAll），插到 custom_action 之后
                ind = indent_of(text, ks)
                ins = f',\n{ind}"custom_action_param": {render(new_params, ind)}'
                edits.append((ve, ve, ins))
            exp["custom_action_param"] = new_params

    if not touched:
        return text, expect, False

    for start, end, repl in sorted(edits, key=lambda e: -e[0]):
        text = text[:start] + repl + text[end:]

    return text, expect, True


def verify(new_text: str, expect: dict, fname: str) -> bool:
    try:
        got = json.loads(new_text)
    except json.JSONDecodeError as e:
        print(f"  ❌ {fname}: 改写后 JSON 非法（{e}）")
        return False
    if got != json.loads(json.dumps(expect)):
        for k in set(got) | set(expect):
            if got.get(k) != expect.get(k):
                print(f"  ❌ {fname}: 节点 {k!r} 与预期不符")
                print(f"     期望: {json.dumps(expect.get(k), ensure_ascii=False)[:300]}")
                print(f"     实际: {json.dumps(got.get(k), ensure_ascii=False)[:300]}")
                break
        return False
    return True


# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只预演）")
    args = ap.parse_args()

    files = sorted(
        f for d in PIPELINE_DIRS if d.exists()
        for f in d.rglob("*.json")
        if not any(part.startswith(".") for part in f.relative_to(d).parts)
    )
    print(f"扫描 {len(files)} 个 pipeline JSON（已按 MaaFW 协议跳过 . 开头的文件/目录）\n")

    touched_files: list[str] = []
    failed = False
    for f in files:
        text = f.read_text(encoding="utf-8")
        try:
            new_text, expect, touched = rewrite_text(text, f.name)
        except (ValueError, json.JSONDecodeError) as e:
            warn(f"{f.name}: 解析失败（{e}），整个文件跳过")
            continue
        if not touched:
            continue
        if not verify(new_text, expect, f.name):
            failed = True
            continue
        touched_files.append(f.name)
        if args.apply:
            f.write_text(new_text, encoding="utf-8")

    rewrites = [c for c in changes if c["kind"] == "rewrite"]
    drops = [c for c in changes if c["kind"] == "drop_action"]
    origins = [c for c in changes if c["kind"] == "drop_origin"]

    print("=" * 78)
    print(f"改写 {len(rewrites)} 处｜删除动作 {len(drops)} 处｜删除手写 origin {len(origins)} 处"
          f"｜涉及 {len(touched_files)} 个文件")
    print("=" * 78)

    by_old: dict[str, int] = {}
    for c in rewrites:
        by_old[c["old"]] = by_old.get(c["old"], 0) + 1
    for k, v in sorted(by_old.items()):
        print(f"  {k:<14} {v:>3} 处")

    print("\n--- 逐处改写 ---")
    for c in rewrites:
        print(f"\n▸ {c['where']}   [{c['old']} -> {c['new']}]")
        print(f"    旧: {json.dumps(c['old_params'], ensure_ascii=False)}")
        print(f"    新: {json.dumps(c['new_params'], ensure_ascii=False)}")

    if drops:
        print("\n--- 删除的动作 ---")
        for c in drops:
            print(f"  ▸ {c['where']}  [{c['old']}]  {c['note']}")

    if origins:
        print(f"\n--- 删除的手写 origin（{len(origins)} 处，改为自动取值）---")
        for c in origins:
            print(f"  ▸ {c['where']}  ({c['key']})")

    if warnings:
        print(f"\n--- 需人工复核（{len(warnings)} 条）---")
        for w in warnings:
            print(f"  ⚠️ {w}")

    print()
    if failed:
        print("❌ 有文件语义校验未通过，已中止写盘。请修脚本后重跑。")
        return 1
    if args.apply:
        print(f"✅ 已写盘 {len(touched_files)} 个文件：{', '.join(touched_files)}")
    else:
        print("（预演模式，未写盘。确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
