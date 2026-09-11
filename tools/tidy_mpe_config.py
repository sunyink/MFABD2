#!/usr/bin/env python3
"""整理 MaaPipelineEditor 的分离配置文件（``.<名字>.mpe.json``）。

配套关系：``pipeline/Foo.json`` <-> ``pipeline/.Foo.mpe.json``。
sidecar 只存画布信息（坐标 / 外部节点 / 锚点 / 分组），一旦与 pipeline JSON
脱节，编辑器就会出现幽灵节点、节点堆在原点、跨类型同名报「节点名重复」。

按 MaaPipelineEditor 的实际实现（Editor/src/core/parser/{configSplitter,importer,
edgeLinker}.ts、Editor/src/stores/flow/utils/nodeUtils.ts）重算 sidecar：

1. ``file_config.filePath`` / ``separatedConfigPath`` 指回本文件所在目录；
   ``filename`` 对齐 pipeline 文件名（导入时按 ``filename.length + 1``
   无条件截断外部/锚点节点名，写错会把节点名截断成乱码）。
2. ``node_configs`` 只保留 pipeline JSON 里真实存在的节点；缺坐标的补一个空位。
3. ``external_nodes`` / ``anchor_nodes`` 由 ``next`` / ``on_error`` / ``interrupt``
   里的悬空引用重算：带 ``[Anchor]`` 前缀或 ``{"anchor": true}`` 的算锚点，
   其余算外部节点。原有坐标保留，新增的自动找空位。
   同类型同名允许多份（编辑器视作视觉副本，存在 ``extra_positions`` 里）；
   跨类型同名才是真冲突。
4. ``group_nodes.childrenLabels`` 过滤到仍然存在的节点；同一节点被多个分组认领时
   只保留第一个（导入时 parentId 后者覆盖前者，等于静默丢分组）；空分组删除。

用法::

    python tools/tidy_mpe_config.py assets/resource/pc/pipeline/Arbitrage.json
    python tools/tidy_mpe_config.py assets/resource/pc/pipeline/Arbitrage.json --write

默认只报告不落盘。``--write`` 才覆盖 sidecar。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

# 编辑器画布上一个节点的大致占位，用来找空位
SLOT_W = 300
SLOT_H = 140

FLOW_FIELDS = ("next", "on_error", "interrupt")
REF_PREFIX_RE = re.compile(r"^(?:\[Anchor\]|\[JumpBack\])+", re.IGNORECASE)


def parse_ref(ref: Any) -> tuple[str | None, bool]:
    """解析一条节点引用，返回 (节点名, 是否锚点)。"""
    if isinstance(ref, dict):
        return ref.get("name"), bool(ref.get("anchor"))
    if not isinstance(ref, str):
        return None, False
    match = REF_PREFIX_RE.match(ref)
    if not match:
        return ref, False
    return ref[len(match.group(0)) :], "[anchor]" in match.group(0).lower()


def collect_dangling_refs(pipeline: dict) -> dict[str, bool]:
    """收集被引用但本文件未定义的节点名 -> 是否锚点（有一处标锚点就算锚点）。"""
    defined = set(pipeline)
    dangling: dict[str, bool] = {}
    for node in pipeline.values():
        if not isinstance(node, dict):
            continue
        for field in FLOW_FIELDS:
            refs = node.get(field)
            if refs is None:
                continue
            if not isinstance(refs, list):
                refs = [refs]
            for ref in refs:
                name, is_anchor = parse_ref(ref)
                if not name or name in defined:
                    continue
                dangling[name] = dangling.get(name, False) or is_anchor
    return dangling


def neighbour_of(name: str, pipeline: dict, placed: dict[str, dict]) -> dict | None:
    """给新节点找一个参照坐标：先找连边邻居，再退化到同前缀命名的兄弟节点。"""
    # 指向本节点的上游
    for src, node in pipeline.items():
        if src not in placed or not isinstance(node, dict):
            continue
        for field in FLOW_FIELDS:
            refs = node.get(field)
            if refs is None:
                continue
            if not isinstance(refs, list):
                refs = [refs]
            if any(parse_ref(r)[0] == name for r in refs):
                return placed[src]

    # 本节点指向的下游
    node = pipeline.get(name)
    if isinstance(node, dict):
        for field in FLOW_FIELDS:
            refs = node.get(field)
            if refs is None:
                continue
            if not isinstance(refs, list):
                refs = [refs]
            for ref in refs:
                target = parse_ref(ref)[0]
                if target in placed:
                    return placed[target]

    # 命名兄弟：公共 _ 分段前缀最长的那个
    segments = name.split("_")
    best, best_len = None, 0
    for other, pos in placed.items():
        other_segments = other.split("_")
        common = 0
        for a, b in zip(segments, other_segments):
            if a != b:
                break
            common += 1
        if common > best_len:
            best, best_len = pos, common
    return best


def find_free_slot(anchor: dict | None, occupied: list[dict]) -> dict:
    """在 anchor 右侧找一个不与已有节点重叠的位置。"""
    if anchor is None:
        # 没有参照就排在整张图右侧
        base_x = max((p["x"] for p in occupied), default=0) + SLOT_W
        base_y = min((p["y"] for p in occupied), default=0)
    else:
        base_x, base_y = anchor["x"] + SLOT_W, anchor["y"]

    def collides(x: int, y: int) -> bool:
        return any(
            abs(p["x"] - x) < SLOT_W * 0.7 and abs(p["y"] - y) < SLOT_H * 0.7
            for p in occupied
        )

    for ring in range(0, 40):
        for dy in ({0} if ring == 0 else {ring, -ring}):
            for dx in range(0, ring + 1):
                x, y = base_x + dx * SLOT_W, base_y + dy * SLOT_H
                if not collides(x, y):
                    return {"x": int(x), "y": int(y)}
    return {"x": int(base_x), "y": int(base_y)}


def tidy(pipeline_path: Path, path_root: str | None = None) -> tuple[dict, list[str]]:
    sidecar_path = pipeline_path.with_name(f".{pipeline_path.stem}.mpe.json")
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    config = json.loads(sidecar_path.read_text(encoding="utf-8"))
    report: list[str] = []

    old_node_configs: dict = config.get("node_configs", {})
    old_external: dict = config.get("external_nodes", {})
    old_anchor: dict = config.get("anchor_nodes", {})
    old_groups: dict = config.get("group_nodes", {})

    # --- file_config ---------------------------------------------------
    file_config = dict(config.get("file_config", {}))
    # 路径按「原 filePath 的根 + 本文件在 assets/resource 下的相对路径」重拼。
    # 不能直接用本机 resolve()：F: 是共享盘的映射，客户端 resolve 出来是 UNC 路径，
    # 而编辑器跑在服务器本机，认的是 F:\Git BD2\... 那套写法。
    tail = pipeline_path.as_posix()
    tail = tail[tail.lower().rindex("assets/resource/") :].replace("/", "\\")
    root = path_root
    if root is None:
        for old in (
            config.get("file_config", {}).get("filePath"),
            config.get("file_config", {}).get("separatedConfigPath"),
        ):
            if old and "assets" in old.lower():
                root = old[: old.lower().rindex("assets\\resource\\")].rstrip("\\")
                break
    if root is None:
        root = str(pipeline_path.parents[3])
    win_pipeline = f"{root}\\{tail}"
    win_sidecar = f"{root}\\{tail.rsplit(chr(92), 1)[0]}\\{sidecar_path.name}"
    if file_config.get("filePath") != win_pipeline:
        report.append(f"filePath: {file_config.get('filePath')!r} -> {win_pipeline!r}")
        file_config["filePath"] = win_pipeline
    if file_config.get("separatedConfigPath") != win_sidecar:
        report.append(
            f"separatedConfigPath: {file_config.get('separatedConfigPath')!r} -> {win_sidecar!r}"
        )
        file_config["separatedConfigPath"] = win_sidecar
    if file_config.get("filename") != pipeline_path.stem:
        report.append(
            f"filename: {file_config.get('filename')!r} -> {pipeline_path.stem!r}"
        )
        file_config["filename"] = pipeline_path.stem
    file_config["coordinateMode"] = file_config.get("coordinateMode", "absolute-v1")
    # 这两个是编辑器的同步状态：sidecar 现在与 pipeline JSON 对齐了，就按一次同步记
    file_config["lastSyncTime"] = int(time.time() * 1000)
    file_config["isModifiedExternally"] = False

    # --- node_configs --------------------------------------------------
    node_configs: dict = {}
    ghosts = [k for k in old_node_configs if k not in pipeline]
    for name in pipeline:
        if name in old_node_configs:
            node_configs[name] = old_node_configs[name]
    missing = [k for k in pipeline if k not in node_configs]

    if ghosts:
        report.append(f"node_configs 删除 {len(ghosts)} 个幽灵条目: {ghosts}")

    # --- external / anchor ---------------------------------------------
    dangling = collect_dangling_refs(pipeline)
    external: dict = {}
    anchor: dict = {}
    for name, is_anchor in dangling.items():
        prev = old_anchor.get(name) if is_anchor else old_external.get(name)
        if prev is None:
            # 换过类型（含从本文件节点降级成外部节点）的，坐标也捡回来用
            prev = (
                old_external.get(name)
                or old_anchor.get(name)
                or old_node_configs.get(name)
            )
        (anchor if is_anchor else external)[name] = prev

    dropped_ext = sorted(set(old_external) - set(external))
    dropped_anc = sorted(set(old_anchor) - set(anchor))
    if dropped_ext:
        report.append(f"external_nodes 删除 {len(dropped_ext)} 个无引用条目: {dropped_ext}")
    if dropped_anc:
        report.append(f"anchor_nodes 删除 {len(dropped_anc)} 个无引用条目: {dropped_anc}")

    # --- 补坐标 ----------------------------------------------------------
    def positions_of(cfg: dict) -> list[dict]:
        out = []
        for entry in cfg.values():
            if not entry:
                continue
            if entry.get("position"):
                out.append(entry["position"])
            out.extend(entry.get("extra_positions", []))
        return out

    placed = {n: e["position"] for n, e in node_configs.items() if e and e.get("position")}
    occupied = positions_of(node_configs) + positions_of(external) + positions_of(anchor)

    for name in missing:
        pos = find_free_slot(neighbour_of(name, pipeline, placed), occupied)
        node_configs[name] = {"position": pos}
        placed[name] = pos
        occupied.append(pos)
        report.append(f"node_configs 补坐标: {name} -> ({pos['x']}, {pos['y']})")

    for bucket, label in ((external, "external_nodes"), (anchor, "anchor_nodes")):
        for name, entry in list(bucket.items()):
            if entry and entry.get("position"):
                continue
            pos = find_free_slot(neighbour_of(name, pipeline, placed), occupied)
            bucket[name] = {"position": pos}
            occupied.append(pos)
            report.append(f"{label} 补坐标: {name} -> ({pos['x']}, {pos['y']})")

    # 按名字排序，减少后续 diff 噪声（编辑器不依赖顺序）
    node_configs = {k: node_configs[k] for k in pipeline if k in node_configs}
    external = dict(sorted(external.items()))
    anchor = dict(sorted(anchor.items()))

    # --- group_nodes ----------------------------------------------------
    known_labels = set(node_configs) | set(external) | set(anchor)
    groups: dict = {}
    claimed: set[str] = set()
    for group_name, group in old_groups.items():
        group = dict(group)
        children = group.get("childrenLabels", [])
        kept, dropped, stolen = [], [], []
        for child in children:
            if child not in known_labels:
                dropped.append(child)
            elif child in claimed:
                stolen.append(child)
            else:
                kept.append(child)
                claimed.add(child)
        if dropped:
            report.append(
                f"分组 {group_name!r} 移除 {len(dropped)} 个已不存在的子节点: {dropped}"
            )
        if stolen:
            report.append(
                f"分组 {group_name!r} 移除被其它分组先认领的子节点: {stolen}"
            )
        if not kept:
            report.append(f"分组 {group_name!r} 子节点已全空，删除该分组")
            continue
        group["childrenLabels"] = kept
        groups[group_name] = group

    result: dict = {"file_config": file_config, "node_configs": node_configs}
    if external:
        result["external_nodes"] = external
    if anchor:
        result["anchor_nodes"] = anchor
    if config.get("sticker_nodes"):
        result["sticker_nodes"] = config["sticker_nodes"]
    if groups:
        result["group_nodes"] = groups

    # --- 判重自检（对齐 nodeUtils.checkRepeatNodeLabelList） -------------
    buckets: dict[str, set[str]] = {}
    for kind, names in (
        ("Pipeline", pipeline.keys()),
        ("External", external.keys()),
        ("Anchor", anchor.keys()),
    ):
        for name in names:
            buckets.setdefault(name, set()).add(kind)
    conflicts = {n: sorted(k) for n, k in buckets.items() if len(k) > 1}
    if conflicts:
        report.append(f"⚠ 仍存在跨类型同名（编辑器会报节点名重复）: {conflicts}")
    else:
        report.append("判重自检通过：无跨类型同名")

    return result, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="整理 MaaPipelineEditor 的分离配置文件（.<名字>.mpe.json）"
    )
    parser.add_argument("pipeline", type=Path, help="pipeline JSON 路径（不是 sidecar）")
    parser.add_argument("--write", action="store_true", help="覆盖写回 sidecar")
    parser.add_argument(
        "--root",
        help="file_config 里路径的根（默认沿用原 filePath 的根，避免把服务器本机路径改成 UNC）",
    )
    args = parser.parse_args()

    pipeline_path = args.pipeline.resolve()
    if not pipeline_path.is_file():
        print(f"找不到 pipeline 文件: {pipeline_path}", file=sys.stderr)
        return 1
    sidecar_path = pipeline_path.with_name(f".{pipeline_path.stem}.mpe.json")
    if not sidecar_path.is_file():
        print(f"找不到 sidecar: {sidecar_path}", file=sys.stderr)
        return 1

    result, report = tidy(pipeline_path, args.root)
    for line in report:
        print(line)

    if args.write:
        sidecar_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
        print(f"\n已写回 {sidecar_path}")
    else:
        print("\n（未落盘，加 --write 才会覆盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
