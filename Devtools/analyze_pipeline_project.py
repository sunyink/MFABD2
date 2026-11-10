#!/usr/bin/env python3
"""Analyze a MaaFramework consumer project and generate basic_info.md.

来源：Everything-Maa (KhazixW2)，MIT，skills/maa-project-init/scripts/，对应上游提交 22cf3c3（2026-11-08）。
本地修改 1 处（analyze_project 里的 resource_bundle_for，搜「本地修改」）：同名节点按资源目录判归属，
否则 PC = base + pc 这类多目录条目会把合法覆盖误报成同包重复。重新同步上游时，上游没修就要带上这处。
纯标准库，只读扫描；不带 --write-basic-info 时不写任何文件。

用法：
    python Devtools/analyze_pipeline_project.py .          # 人读的 Markdown 报告
    python Devtools/analyze_pipeline_project.py . --json   # 机器可读，喂给别的脚本
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REFERENCE_FIELDS = ("next", "on_error", "interrupt")
FLOW_MAX_DEPTH = 5
FLOW_MAX_EDGES = 32
FLOW_MAX_NODES = 36
FLOW_MAX_TASKS = 20
COMMON_NODE_RE = re.compile(
    r"(Back|Return|Exit|Close|Closed|Logout|Stop|Confirm|Cancel|Retry|Wait|"
    r"Flag|Popup|Start|Save|Home|Loading|Communicat|PowerLack|Check)",
    re.IGNORECASE,
)
RETURN_NODE_RE = re.compile(
    r"(Back|Return|Exit|Close|Closed|Logout|Stop|Leave)", re.IGNORECASE
)
CONFIRM_NODE_RE = re.compile(r"(Confirm|Cancel|Retry|OK|Yes|No)", re.IGNORECASE)
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".nicegui",
    ".playwright-mcp",
}

# Agent 入口扫描：与运行时 maa_mcp/agent_supervisor._build_subprocess_cmd 保持同步
# 若以后修改运行时的 ancestor 上溯层数，必须同步修改此常量。
AGENT_PARENT_WALK_LIMIT = 4

# 常见 agent 入口文件名（按出现频率排序），用于主动枚举仓库内的候选入口
AGENT_ENTRY_BASENAMES = (
    "main.py",
    "server.py",
    "agent.py",
    "run.py",
    "app.py",
)

# 常见 agent 目录名
AGENT_DIR_NAMES = ("agent", "agents", "Agent", "Agents")

# Top-level fields permitted by M9A's interface_import.schema.json. Nested
# task declarations may carry their own group and option references.
INTERFACE_IMPORT_LIST_FIELDS = (
    "task",
    "preset",
)
INTERFACE_IMPORT_MAP_FIELDS = ("option",)
WILDCARD_IMPORT_CHARS = ("*", "?", "[")


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    field: str
    source_file: str
    attrs: tuple[str, ...] = ()


def load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_normalize_jsonc(text))


def _normalize_jsonc(text: str) -> str:
    return _strip_jsonc_trailing_commas(_strip_jsonc_comments(text))


def _strip_jsonc_comments(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    i = 0

    while i < len(text):
        char = text[i]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            output.append(char)
            i += 1
        elif char == "/" and i + 1 < len(text) and text[i + 1] == "/":
            start = i
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            output.extend(" " * (i - start))
        elif char == "/" and i + 1 < len(text) and text[i + 1] == "*":
            start = i
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            if i + 1 >= len(text):
                raise ValueError("unterminated block comment")
            i += 2
            output.extend(
                "\n" if text[position] == "\n" else " "
                for position in range(start, i)
            )
        else:
            output.append(char)
            i += 1

    return "".join(output)


def _strip_jsonc_trailing_commas(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False

    for i, char in enumerate(text):
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            output.append(char)
            continue
        if char == ",":
            next_index = i + 1
            while next_index < len(text) and text[next_index].isspace():
                next_index += 1
            if next_index < len(text) and text[next_index] in "]}":
                continue
        output.append(char)

    return "".join(output)


def load_interface_bundle(interface_path: Path | None) -> tuple[dict, dict]:
    """Load the main Interface and merge its direct import declarations."""
    diagnostics: dict = {
        "main_path": portable_path(interface_path) if interface_path else "",
        "declared": [],
        "loaded": [],
        "missing": [],
        "invalid": [],
        "warnings": [],
    }
    interface: dict = {}
    if not interface_path:
        return interface, diagnostics

    try:
        loaded = load_json(interface_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        diagnostics["warnings"].append(f"无法读取主 Interface: {error}")
        return interface, diagnostics
    if not isinstance(loaded, dict):
        diagnostics["warnings"].append("主 Interface 顶层不是 JSON object")
        return interface, diagnostics
    interface = loaded
    _normalize_interface_shapes(interface, diagnostics)

    imports = interface.get("import") or []
    if not isinstance(imports, list):
        diagnostics["warnings"].append("主 Interface 的 import 字段不是数组")
        imports = []

    base_dir = interface_path.parent
    for raw_path in imports:
        if not isinstance(raw_path, str) or not raw_path:
            diagnostics["invalid"].append(str(raw_path))
            diagnostics["warnings"].append("Interface import 包含非字符串或空路径")
            continue

        diagnostics["declared"].append(raw_path)
        try:
            import_paths = _expand_interface_import(base_dir, raw_path)
        except ValueError as error:
            diagnostics["invalid"].append(raw_path)
            diagnostics["warnings"].append(f"Interface import `{raw_path}` 无效: {error}")
            continue

        if not import_paths:
            diagnostics["missing"].append(raw_path)
            diagnostics["warnings"].append(f"Interface import `{raw_path}` 没有匹配到任何文件")
            continue

        for import_path in import_paths:
            loaded_name = _import_diagnostic_name(base_dir, import_path, raw_path)
            try:
                imported = load_json(import_path)
            except (OSError, json.JSONDecodeError, ValueError) as error:
                diagnostics["missing"].append(loaded_name)
                diagnostics["warnings"].append(
                    f"无法读取 Interface import `{loaded_name}`: {error}"
                )
                continue
            if not isinstance(imported, dict):
                diagnostics["invalid"].append(loaded_name)
                diagnostics["warnings"].append(
                    f"Interface import `{loaded_name}` 顶层不是 JSON object"
                )
                continue

            diagnostics["loaded"].append(loaded_name)
            for field in INTERFACE_IMPORT_LIST_FIELDS:
                values = imported.get(field) or []
                if not isinstance(values, list):
                    diagnostics["warnings"].append(
                        f"Interface import `{loaded_name}` 的 {field} 字段不是数组"
                    )
                    continue
                interface.setdefault(field, []).extend(values)
            for field in INTERFACE_IMPORT_MAP_FIELDS:
                values = imported.get(field) or {}
                if not isinstance(values, dict):
                    diagnostics["warnings"].append(
                        f"Interface import `{loaded_name}` 的 {field} 字段不是 object"
                    )
                    continue
                interface.setdefault(field, {}).update(values)

    return interface, diagnostics


def _normalize_interface_shapes(interface: dict, diagnostics: dict) -> None:
    for field in INTERFACE_IMPORT_LIST_FIELDS:
        value = interface.get(field)
        if value is not None and not isinstance(value, list):
            diagnostics["warnings"].append(f"主 Interface 的 {field} 字段不是数组")
            interface[field] = []
    for field in INTERFACE_IMPORT_MAP_FIELDS:
        value = interface.get(field)
        if value is not None and not isinstance(value, dict):
            diagnostics["warnings"].append(f"主 Interface 的 {field} 字段不是 object")
            interface[field] = {}

    resources = interface.get("resource")
    if resources is None:
        return
    if not isinstance(resources, list):
        diagnostics["warnings"].append("主 Interface 的 resource 字段不是数组")
        interface["resource"] = []
        return

    valid_groups: list[dict] = []
    for index, group in enumerate(resources):
        if not isinstance(group, dict):
            diagnostics["warnings"].append(
                f"主 Interface 的 resource[{index}] 不是 JSON object"
            )
            continue

        raw_paths = group.get("path")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, list):
            diagnostics["warnings"].append(
                f"主 Interface 的 resource[{index}].path 不是字符串或字符串数组"
            )
            raw_paths = []
        else:
            valid_paths = [
                path for path in raw_paths if isinstance(path, str) and bool(path)
            ]
            if len(valid_paths) != len(raw_paths):
                diagnostics["warnings"].append(
                    f"主 Interface 的 resource[{index}].path 不是字符串或字符串数组"
                )
            raw_paths = valid_paths
        name = group.get("name")
        normalized_name = name if isinstance(name, str) and name else "<unnamed>"
        group = {**group, "name": normalized_name, "path": raw_paths}
        valid_groups.append(group)
    interface["resource"] = valid_groups


def _expand_interface_import(base_dir: Path, raw_path: str) -> list[Path]:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ValueError("import 必须是相对于主 Interface 的路径")
    if not any(char in raw_path for char in WILDCARD_IMPORT_CHARS):
        return [(base_dir / candidate).resolve()]
    return sorted(
        path.resolve()
        for path in base_dir.glob(raw_path)
        if path.is_file()
    )


def _import_diagnostic_name(base_dir: Path, import_path: Path, raw_path: str) -> str:
    if not any(char in raw_path for char in WILDCARD_IMPORT_CHARS):
        return raw_path
    try:
        return portable_path(import_path.relative_to(base_dir))
    except ValueError:
        return portable_path(import_path)


def rel(path: Path, root: Path) -> str:
    try:
        return portable_path(path.relative_to(root))
    except ValueError:
        return portable_path(path)


def portable_path(path: Path) -> str:
    return path.as_posix()


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def find_interface(project_root: Path) -> Path | None:
    candidates = [
        project_root / "interface.json",
        project_root / "interface.jsonc",
        project_root / "assets" / "interface.json",
        project_root / "assets" / "interface.jsonc",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    found = [
        p
        for pattern in ("interface.json", "interface.jsonc")
        for p in project_root.rglob(pattern)
        if not should_skip(p) and "deps" not in p.parts and "install" not in p.parts
    ]
    if not found:
        return None
    return sorted(found, key=lambda p: (len(p.parts), str(p)))[0]


def resolve_resource_dirs(project_root: Path, interface_path: Path | None, interface: dict) -> list[dict]:
    if not interface_path:
        return []

    base_dir = interface_path.parent
    resources = interface.get("resource") or []
    if not isinstance(resources, list):
        return []

    groups: list[dict] = []
    for group in resources:
        if not isinstance(group, dict):
            continue
        raw_paths = group.get("path") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, list):
            raw_paths = []
        raw_paths = [path for path in raw_paths if isinstance(path, str) and bool(path)]
        resolved = [(base_dir / p).resolve() for p in raw_paths]
        name = group.get("name")
        groups.append(
            {
                "name": name if isinstance(name, str) and name else "<unnamed>",
                "raw_paths": list(raw_paths),
                "paths": [portable_path(p) for p in resolved],
                "existing_paths": [portable_path(p) for p in resolved if p.is_dir()],
            }
        )
    return groups


def unique_existing_resource_dirs(resource_groups: list[dict]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for group in resource_groups:
        for value in group.get("existing_paths", []):
            path = Path(value)
            key = os.path.normcase(str(path))
            if key not in seen:
                try:
                    duplicate = path.is_dir() and any(
                        path.samefile(existing) for existing in result if existing.is_dir()
                    )
                except OSError:
                    duplicate = False
                if duplicate:
                    continue
                seen.add(key)
                result.append(path)
    return result


def discover_pipeline_files(project_root: Path, resource_dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    pipeline_files: set[Path] = set()
    default_files: set[Path] = set()
    for resource_dir in resource_dirs:
        default = resource_dir / "default_pipeline.json"
        if default.is_file():
            default_files.add(default)
        pipeline_dir = resource_dir / "pipeline"
        if pipeline_dir.is_dir():
            pipeline_files.update(p for p in pipeline_dir.rglob("*.json") if p.is_file())

    return sorted(pipeline_files), sorted(default_files)


def discover_image_files(resource_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for resource_dir in resource_dirs:
        image_dir = resource_dir / "image"
        if image_dir.is_dir():
            files.extend(p for p in image_dir.rglob("*") if p.is_file())
    return sorted(files)


def strip_prefixed_ref(value: str) -> tuple[str, tuple[str, ...]]:
    attrs: list[str] = []
    text = value
    while text.startswith("["):
        end = text.find("]")
        if end <= 0:
            break
        attrs.append(text[1:end])
        text = text[end + 1 :]
    return text, tuple(attrs)


def iter_refs(value: Any) -> list[tuple[str, tuple[str, ...]]]:
    refs: list[tuple[str, tuple[str, ...]]] = []
    if value is None:
        return refs
    if isinstance(value, str):
        target, attrs = strip_prefixed_ref(value)
        if target:
            refs.append((target, attrs))
        return refs
    if isinstance(value, list):
        for item in value:
            refs.extend(iter_refs(item))
        return refs
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name:
            attrs = tuple(
                key
                for key in ("jump_back", "anchor")
                if value.get(key) is True
            )
            refs.append((name, attrs))
        return refs
    return refs


def collect_anchor_defs(node_name: str, node: dict) -> list[dict]:
    """Parse a node-level `anchor` field into anchor-name declarations.

    Distinct from the `next` element's boolean `anchor` attribute (handled by
    `iter_refs`), the Pipeline protocol's node-level `anchor` field declares an
    anchor *name* that `[Anchor]X` references resolve against at runtime.
    It is `string | list | object`:

    - `"anchor": "Cooking"` — anchor `Cooking` now targets this node.
    - `"anchor": ["A", "B"]` — several anchors now target this node.
    - `"anchor": {"Cooking": "TargetNode"}` — object form (v5.7+): explicit
      target, which need not be this node.
    - `"anchor": {"Cooking": ""}` — clears the anchor; the name stays declared
      but currently has no target.
    """
    raw = node.get("anchor")
    if raw is None:
        return []
    defs: list[dict] = []
    if isinstance(raw, str):
        if raw:
            defs.append({"anchor": raw, "target": node_name, "cleared": False})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item:
                defs.append({"anchor": item, "target": node_name, "cleared": False})
    elif isinstance(raw, dict):
        for anchor_name, target in raw.items():
            if not isinstance(anchor_name, str) or not anchor_name:
                continue
            if isinstance(target, str) and target:
                defs.append({"anchor": anchor_name, "target": target, "cleared": False})
            else:
                # Empty string / null clears the anchor; the name is still declared.
                defs.append({"anchor": anchor_name, "target": None, "cleared": True})
    return defs


def is_anchor_attr(attrs: tuple[str, ...]) -> bool:
    """True when a reference's attrs flag it as anchor-routed.

    Case-insensitive: the `[Anchor]` prefix yields the attr `"Anchor"` (see
    `strip_prefixed_ref`), while the `next` element object form
    `{"name": ..., "anchor": true}` yields the attr `"anchor"`. Both mean the
    reference's target string is an anchor *name*, not a literal node name.
    """
    return any(attr.lower() == "anchor" for attr in attrs)


def display_value(value: Any, max_len: int = 96) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def md_escape(value: Any) -> str:
    return display_value(value).replace("|", "\\|")


def node_action_name(node: dict) -> str:
    action = node.get("action", "<default>")
    if isinstance(action, str):
        return action
    if isinstance(action, dict):
        return str(action.get("type") or action.get("action") or "<object>")
    return str(action)


def node_action_params(node: dict) -> dict:
    action = node.get("action")
    if not isinstance(action, dict):
        return node
    params = action.get("param")
    return params if isinstance(params, dict) else action


def node_custom_action_name(node: dict) -> str:
    """Return the CustomAction registration name for v1 or v2 node syntax."""
    if node_action_name(node).lower() != "custom":
        return ""
    value = node_action_params(node).get("custom_action")
    return value if isinstance(value, str) else ""


def node_recognition_name(node: dict) -> str:
    recognition = node.get("recognition", "<default>")
    if isinstance(recognition, str):
        return recognition
    if isinstance(recognition, dict):
        return str(recognition.get("type") or recognition.get("recognition") or "<object>")
    return str(recognition)


def node_recognition_params(node: dict) -> dict:
    recognition = node.get("recognition")
    if not isinstance(recognition, dict):
        return node
    params = recognition.get("param")
    return params if isinstance(params, dict) else recognition


def is_android_back_key(node: dict) -> bool:
    if node_action_name(node).lower() != "clickkey":
        return False
    key = node_action_params(node).get("key")
    return key == 4 or key == [4]


def classify_node(name: str, node: dict, in_degree: int) -> list[str]:
    categories: list[str] = []
    if in_degree >= 5:
        categories.append("high-in-degree")
    if RETURN_NODE_RE.search(name) or is_android_back_key(node):
        categories.append("return-exit")
    if CONFIRM_NODE_RE.search(name):
        categories.append("confirm-cancel")
    if COMMON_NODE_RE.search(name):
        categories.append("common-ui")
    if node_recognition_name(node) == "TemplateMatch":
        template = display_value(node_recognition_params(node).get("template"))
        if RETURN_NODE_RE.search(template) or CONFIRM_NODE_RE.search(template):
            categories.append("template-control")
    return sorted(set(categories))


def analyze_pipeline_files(
    project_root: Path,
    pipeline_files: list[Path],
    resource_dirs: list[Path] | None = None,
    resource_bundle_for: dict[str, str] | None = None,
) -> dict:
    node_defs: dict[str, list[dict]] = defaultdict(list)
    file_summaries: list[dict] = []
    edges: list[Edge] = []
    read_errors: list[dict] = []
    templates: list[dict] = []
    ocr_expected: list[dict] = []
    roi_nodes: list[dict] = []
    custom_action_nodes: list[dict] = []
    anchor_defs: list[dict] = []
    resource_roots = [Path(path).resolve() for path in resource_dirs or []]
    bundle_by_root = {
        portable_path(Path(path).resolve()): bundle
        for path, bundle in (resource_bundle_for or {}).items()
    }

    def resource_for(path: Path) -> str:
        resolved = path.resolve()
        for resource_root in resource_roots:
            try:
                resolved.relative_to(resource_root)
            except ValueError:
                continue
            return bundle_by_root.get(
                portable_path(resource_root), portable_path(resource_root)
            )
        return "<unknown>"

    for path in pipeline_files:
        rel_file = rel(path, project_root)
        try:
            data = load_json(path)
        except Exception as exc:  # noqa: BLE001 - analyzer should report all read issues.
            read_errors.append({"file": rel_file, "error": str(exc)})
            continue
        if not isinstance(data, dict):
            read_errors.append({"file": rel_file, "error": "top-level JSON is not an object"})
            continue

        file_summaries.append({"file": rel_file, "node_count": len(data)})
        for name, raw_node in data.items():
            node = raw_node if isinstance(raw_node, dict) else {"value": raw_node}
            recognition_params = node_recognition_params(node)
            node_defs[name].append(
                {
                    "file": rel_file,
                    "resource": resource_for(path),
                    "recognition": node_recognition_name(node),
                    "action": node_action_name(node),
                    "node": node,
                }
            )

            custom_action = node_custom_action_name(node)
            if custom_action:
                custom_action_nodes.append(
                    {
                        "node": name,
                        "custom_action": custom_action,
                        "file": rel_file,
                    }
                )

            for field in REFERENCE_FIELDS:
                for target, attrs in iter_refs(node.get(field)):
                    edges.append(Edge(name, target, field, rel_file, attrs))

            anchor_defs.extend(
                {**entry, "file": rel_file} for entry in collect_anchor_defs(name, node)
            )

            if "template" in recognition_params:
                templates.append(
                    {
                        "node": name,
                        "file": rel_file,
                        "template": recognition_params.get("template"),
                        "roi": recognition_params.get("roi"),
                    }
                )
            if "expected" in recognition_params:
                ocr_expected.append(
                    {
                        "node": name,
                        "file": rel_file,
                        "expected": recognition_params.get("expected"),
                        "roi": recognition_params.get("roi"),
                    }
                )
            if "roi" in recognition_params:
                roi_nodes.append(
                    {
                        "node": name,
                        "file": rel_file,
                        "roi": recognition_params.get("roi"),
                    }
                )

    node_names = set(node_defs)

    # Node-level `anchor` declarations: anchor name -> set of target nodes.
    # One anchor name may legitimately map to many targets (whichever node
    # declared it last wins at runtime), so this is name -> set(targets), not
    # a 1:1 mapping, and a multi-target anchor is never a conflict.
    anchor_names = {entry["anchor"] for entry in anchor_defs}
    anchor_targets: dict[str, set[str]] = defaultdict(set)
    for entry in anchor_defs:
        if not entry["cleared"]:
            anchor_targets[entry["anchor"]].add(entry["target"])

    # Failure mode (b): a declared anchor's explicit (object-form) target does
    # not exist as a node. String/list forms always self-target the declaring
    # node, so they can never be dangling by construction.
    dangling_target_keys = sorted(
        {
            (entry["anchor"], entry["target"], entry["file"])
            for entry in anchor_defs
            if not entry["cleared"] and entry["target"] not in node_names
        }
    )
    dangling_anchor_targets = [
        {"anchor": anchor, "target": target, "file": file}
        for anchor, target, file in dangling_target_keys
    ]

    # Redirect anchor-flagged edges (`[Anchor]X` / `{"anchor": true}`) through
    # the anchor table so they connect to their declared target node(s).
    # Failure mode (a): the anchor name itself was never declared anywhere.
    resolved_edges: list[Edge] = []
    unresolved_anchor_ref_names: set[str] = set()
    for edge in edges:
        if not is_anchor_attr(edge.attrs):
            resolved_edges.append(edge)
            continue
        anchor_name = edge.target
        if anchor_name not in anchor_names:
            unresolved_anchor_ref_names.add(anchor_name)
            continue
        for target in sorted(anchor_targets.get(anchor_name, ())):
            if target in node_names:
                resolved_edges.append(
                    Edge(edge.source, target, edge.field, edge.source_file, edge.attrs)
                )
    edges = resolved_edges
    unresolved_anchor_refs = sorted(unresolved_anchor_ref_names)

    in_degree = Counter(edge.target for edge in edges)
    out_degree = Counter(edge.source for edge in edges)
    edge_type_counts = Counter(edge.field for edge in edges)
    unresolved = sorted({edge.target for edge in edges if edge.target not in node_names})
    zero_in_degree = sorted(name for name in node_names if in_degree[name] == 0)
    isolated = sorted(
        name for name in node_names if in_degree[name] == 0 and out_degree[name] == 0
    )
    duplicate_nodes = sorted(
        name
        for name, defs in node_defs.items()
        if len(defs) > len({definition["resource"] for definition in defs})
    )
    cross_bundle_override_nodes = sorted(
        name
        for name, defs in node_defs.items()
        if len({definition["resource"] for definition in defs}) > 1
    )
    cycles = find_cycle_candidates(node_names, edges)

    common_nodes = []
    return_exit_nodes = []
    confirm_nodes = []
    for name, defs in node_defs.items():
        primary = defs[0]["node"]
        categories = classify_node(name, primary, in_degree[name])
        item = {
            "name": name,
            "categories": categories,
            "in_degree": in_degree[name],
            "out_degree": out_degree[name],
            "recognition": defs[0]["recognition"],
            "action": defs[0]["action"],
            "files": [d["file"] for d in defs[:3]],
        }
        if "high-in-degree" in categories or "common-ui" in categories:
            common_nodes.append(item)
        if "return-exit" in categories:
            return_exit_nodes.append(item)
        if "confirm-cancel" in categories:
            confirm_nodes.append(item)

    common_nodes.sort(key=lambda x: (-x["in_degree"], x["name"]))
    return_exit_nodes.sort(key=lambda x: (-x["in_degree"], x["name"]))
    confirm_nodes.sort(key=lambda x: (-x["in_degree"], x["name"]))
    file_summaries.sort(key=lambda x: (-x["node_count"], x["file"]))

    cross_file_edges = []
    for edge in edges:
        target_defs = node_defs.get(edge.target) or []
        target_files = {d["file"] for d in target_defs}
        if target_files and edge.source_file not in target_files:
            cross_file_edges.append(asdict(edge))

    return {
        "node_count": len(node_defs),
        "node_definition_count": sum(len(defs) for defs in node_defs.values()),
        "node_names": sorted(node_defs),
        "duplicate_nodes": duplicate_nodes,
        "cross_bundle_override_nodes": cross_bundle_override_nodes,
        "file_summaries": file_summaries,
        "edges": [asdict(edge) for edge in edges],
        "edge_type_counts": dict(edge_type_counts),
        "cross_file_edges": cross_file_edges,
        "unresolved_refs": unresolved,
        "anchor_names": sorted(anchor_names),
        "unresolved_anchor_refs": unresolved_anchor_refs,
        "dangling_anchor_targets": dangling_anchor_targets,
        "zero_in_degree_nodes": zero_in_degree,
        "isolated_nodes": isolated,
        "cycle_candidates": cycles,
        "common_nodes": common_nodes[:80],
        "return_exit_nodes": return_exit_nodes[:80],
        "confirm_nodes": confirm_nodes[:80],
        "top_in_degree": [
            {"name": name, "in_degree": count}
            for name, count in in_degree.most_common(80)
        ],
        "templates": templates,
        "ocr_expected": ocr_expected,
        "roi_nodes": roi_nodes,
        "custom_action_nodes": custom_action_nodes,
        "read_errors": read_errors,
    }


def find_cycle_candidates(node_names: set[str], edges: list[Edge]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = {name: set() for name in node_names}
    for edge in edges:
        if edge.source in node_names and edge.target in node_names:
            adjacency[edge.source].add(edge.target)

    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in adjacency[node]:
            if target not in indices:
                strongconnect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                current = stack.pop()
                on_stack.remove(current)
                component.append(current)
                if current == node:
                    break
            if len(component) > 1 or node in adjacency[node]:
                components.append(sorted(component))

    for node in sorted(node_names):
        if node not in indices:
            strongconnect(node)

    components.sort(key=lambda item: (-len(item), item))
    return components[:30]


def resolve_agent_arg(root: Path, arg: str) -> dict:
    """对一条 child_args 条目走与运行时相同的解析路径，返回结构化报告。

    镜像 maa_mcp/agent_supervisor._build_subprocess_cmd 的解析语义。
    """
    if not arg or not isinstance(arg, str):
        return {
            "arg": str(arg) if arg is not None else "",
            "is_absolute": False,
            "is_py": False,
            "candidates": [],
            "resolved": None,
            "resolved_at_level": -1,
            "status": "non-py",
        }

    is_absolute = Path(arg).is_absolute()
    is_py = arg.lower().endswith(".py")

    if is_absolute:
        abs_path = Path(arg)
        try:
            resolved_str = str(abs_path.resolve()) if abs_path.is_file() else None
        except OSError:
            resolved_str = None
        return {
            "arg": arg,
            "is_absolute": True,
            "is_py": is_py,
            "candidates": [str(abs_path)],
            "resolved": resolved_str,
            "resolved_at_level": 0 if resolved_str is not None else -1,
            "status": "absolute",
        }

    if not is_py:
        return {
            "arg": arg,
            "is_absolute": False,
            "is_py": False,
            "candidates": [],
            "resolved": None,
            "resolved_at_level": -1,
            "status": "non-py",
        }

    # 与运行时一致：root/arg + root.parents[:AGENT_PARENT_WALK_LIMIT]/arg
    candidates: list[Path] = [root / arg]
    candidates.extend(parent / arg for parent in list(root.parents)[:AGENT_PARENT_WALK_LIMIT])
    resolved_path: Path | None = None
    resolved_level = -1
    for level, candidate in enumerate(candidates):
        try:
            if candidate.is_file():
                resolved_path = candidate
                resolved_level = level
                break
        except OSError:
            continue

    resolved_str: str | None = None
    if resolved_path is not None:
        try:
            resolved_str = str(resolved_path.resolve())
        except OSError:
            resolved_str = str(resolved_path)

    return {
        "arg": arg,
        "is_absolute": False,
        "is_py": True,
        "candidates": [str(c) for c in candidates],
        "resolved": resolved_str,
        "resolved_at_level": resolved_level,
        "status": "resolved" if resolved_path is not None else "unresolved",
    }


def discover_agent_candidates(root: Path) -> list[dict]:
    """主动枚举：root 与其 4 层 ancestor 内，按约定入口名扫描候选 .py。

    不递归子目录（避免误中 agent/action/*.py 等业务模块）。
    candidate 字段为 resolve() 后的绝对路径，便于 cross-check 与 declared 字符串对齐。
    """
    if root is None:
        return []
    roots = [root, *list(root.parents)[:AGENT_PARENT_WALK_LIMIT]]
    seen: set[str] = set()
    results: list[dict] = []
    for level, ancestor in enumerate(roots):
        if should_skip(ancestor):
            continue
        for dir_name in AGENT_DIR_NAMES:
            agent_dir = ancestor / dir_name
            for basename in AGENT_ENTRY_BASENAMES:
                candidate = agent_dir / basename
                try:
                    normalized = str(candidate.resolve())
                except OSError:
                    normalized = str(candidate)
                key = os.path.normcase(normalized)
                if key in seen:
                    continue
                try:
                    duplicate = candidate.is_file() and any(
                        candidate.samefile(Path(existing["candidate"]))
                        for existing in results
                        if existing["exists"]
                    )
                except OSError:
                    duplicate = False
                if duplicate:
                    continue
                seen.add(key)
                try:
                    exists = candidate.is_file()
                except OSError:
                    exists = False
                results.append(
                    {
                        "candidate": normalized,
                        "exists": exists,
                        "level": level,
                    }
                )
    return results


def analyze_agent_scripts(
    project_root: Path,
    interface_path: Path | None,
    agent_block: Any,
) -> dict:
    """组合 declared（child_args 解析）+ discovered（约定入口枚举）视图。

    返回空骨架的场景是无 interface.json 或无 agent 块。agent 块存在但
    内容为空/非法时保留 agent_block_present，并输出 warnings。

    返回的 dict 永远是完整字段集合，便于 render_basic_info / render_summary 直接索引。
    """
    empty: dict = {
        "interface_path": rel(interface_path, project_root) if interface_path else "",
        "agent_block_present": False,
        "agent_config_count": 0,
        "declared": [],
        "discovered": [],
        "declared_resolved": [],
        "declared_resolved_count": 0,
        "declared_unresolved_count": 0,
        "discovered_existing_count": 0,
        "orphan_declarations": [],
        "unused_candidates": [],
        "warnings": [],
    }

    if not interface_path:
        return empty

    warnings: list[str] = []
    if agent_block is None:
        return empty

    if isinstance(agent_block, list):
        agent_configs = []
        for index, item in enumerate(agent_block):
            if isinstance(item, dict):
                agent_configs.append(item)
            else:
                warnings.append(f"interface.json 的 agent[{index}] 不是 JSON object")
        if not agent_block:
            warnings.append("interface.json 的 agent 数组为空")
    elif isinstance(agent_block, dict):
        agent_configs = [agent_block]
    else:
        agent_configs = []
        warnings.append("interface.json 的 agent 字段既不是 object 也不是数组")

    if not interface_path or not agent_configs:
        return {
            **empty,
            "agent_block_present": True,
            "warnings": warnings,
        }

    root = interface_path.parent
    child_args: list[Any] = []
    malformed_child_args = False
    for config in agent_configs:
        args = config.get("child_args")
        if isinstance(args, list):
            child_args.extend(args)
        else:
            malformed_child_args = True

    declared = [resolve_agent_arg(root, str(a)) for a in child_args]
    discovered = discover_agent_candidates(root)

    declared_resolved_set: set[str] = set()
    for item in declared:
        if item.get("resolved"):
            declared_resolved_set.add(item["resolved"])

    discovered_existing_set: set[str] = {
        item["candidate"] for item in discovered if item.get("exists")
    }

    # The Interface declaration is authoritative. A valid entry may have any
    # filename (for example agent/bootstrap.py), so naming is not an orphan signal.
    orphan_declarations: list[str] = []
    unused_candidates = sorted(discovered_existing_set - declared_resolved_set)
    unresolved_args = [item["arg"] for item in declared if item.get("status") == "unresolved"]
    script_count = sum(1 for item in declared if item.get("is_py"))

    if malformed_child_args:
        warnings.append("interface.json 的某个 agent 配置里 child_args 不是数组")
    if not child_args:
        warnings.append("interface.json 的 agent 块里完全没有 child_args 条目")
    elif script_count == 0:
        warnings.append("interface.json 的 agent 块里没有任何 .py 入口条目")

    for arg in unresolved_args:
        warnings.append(f"声明的 `{arg}` 在 project_root 与 4 层 ancestor 内解析不到任何 .py 文件")

    return {
        "interface_path": rel(interface_path, project_root),
        "agent_block_present": True,
        "agent_config_count": len(agent_configs),
        "declared": declared,
        "discovered": discovered,
        "declared_resolved": sorted(declared_resolved_set),
        "declared_resolved_count": len(declared_resolved_set),
        "declared_unresolved_count": len(unresolved_args),
        "discovered_existing_count": len(discovered_existing_set),
        "orphan_declarations": orphan_declarations,
        "unused_candidates": unused_candidates,
        "warnings": warnings,
    }


class PipelineCallVisitor(ast.NodeVisitor):
    """Collect statically named Pipeline entries called by Python code."""

    CALL_METHODS = {"run_task", "run_recognition"}

    def __init__(self, source_file: str) -> None:
        self.source_file = source_file
        self.scopes: list[str] = []
        self.calls: list[dict] = []
        self.dynamic_calls: list[dict] = []
        self.custom_action_registrations: list[dict] = []

    def collect_custom_action_registration(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr != "custom_action":
                continue
            if not (
                decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                continue
            self.custom_action_registrations.append(
                {
                    "name": decorator.args[0].value,
                    "file": self.source_file,
                    "line": decorator.lineno,
                    "handler": ".".join([*self.scopes, node.name]),
                }
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        self.collect_custom_action_registration(node)
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API
        self.collect_custom_action_registration(node)
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if method in self.CALL_METHODS:
            item = {
                "kind": method,
                "file": self.source_file,
                "line": node.lineno,
                "caller": ".".join(self.scopes) or "<module>",
            }
            target = node.args[0].value if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ) else ""
            if target:
                self.calls.append({**item, "target": target})
            else:
                self.dynamic_calls.append(item)
        self.generic_visit(node)


def analyze_python_pipeline_calls(project_root: Path) -> dict:
    agent_dir = project_root / "agent"
    if not agent_dir.is_dir():
        return {
            "python_file_count": 0,
            "calls": [],
            "call_summaries": [],
            "dynamic_calls": [],
            "custom_action_registrations": [],
            "targets": [],
            "read_errors": [],
        }

    calls: list[dict] = []
    dynamic_calls: list[dict] = []
    custom_action_registrations: list[dict] = []
    read_errors: list[dict] = []
    python_files = [
        path for path in agent_dir.rglob("*.py") if not should_skip(path)
    ]
    for path in sorted(python_files):
        source_file = rel(path, project_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:  # noqa: BLE001 - report all onboarding scan issues.
            read_errors.append({"file": source_file, "error": str(exc)})
            continue
        visitor = PipelineCallVisitor(source_file)
        visitor.visit(tree)
        calls.extend(visitor.calls)
        dynamic_calls.extend(visitor.dynamic_calls)
        custom_action_registrations.extend(visitor.custom_action_registrations)

    grouped_calls: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in calls:
        grouped_calls[(item["kind"], item["target"])].append(item)
    call_summaries = []
    for (kind, target), items in grouped_calls.items():
        call_summaries.append(
            {
                "kind": kind,
                "target": target,
                "count": len(items),
                "callers": sorted({item["caller"] for item in items})[:3],
                "locations": [
                    f"{item['file']}:{item['line']}" for item in items[:3]
                ],
            }
        )
    call_summaries.sort(key=lambda item: (-item["count"], item["target"], item["kind"]))

    return {
        "python_file_count": len(python_files),
        "calls": calls,
        "call_summaries": call_summaries,
        "dynamic_calls": dynamic_calls,
        "custom_action_registrations": sorted(
            custom_action_registrations,
            key=lambda item: (item["name"], item["file"], item["line"]),
        ),
        "targets": sorted({item["target"] for item in calls}),
        "read_errors": read_errors,
    }


def trace_primary_path(
    entry: str,
    adjacency: dict[str, list[dict]],
    node_names: set[str],
    max_steps: int = 12,
) -> list[str]:
    if not entry:
        return []
    if entry not in node_names:
        return [entry, "(missing entry)"]

    path: list[str] = []
    seen: set[str] = set()
    current = entry
    for _ in range(max_steps):
        path.append(current)
        if current in seen:
            path.append("(cycle)")
            break
        seen.add(current)

        next_edges = [
            edge for edge in adjacency.get(current, []) if edge.get("field") == "next"
        ]
        if not next_edges:
            break

        current = next_edges[0]["target"]
        if current not in node_names:
            path.extend([current, "(unresolved)"])
            break
    return path


def build_task_flow_graphs(
    tasks: list[dict],
    pipeline: dict,
    max_depth: int = FLOW_MAX_DEPTH,
    max_edges: int = FLOW_MAX_EDGES,
    max_nodes: int = FLOW_MAX_NODES,
    max_tasks: int = FLOW_MAX_TASKS,
) -> list[dict]:
    node_names = set(pipeline.get("node_names") or [])
    unresolved = set(pipeline.get("unresolved_refs") or [])
    adjacency: dict[str, list[dict]] = defaultdict(list)
    for edge in pipeline.get("edges", []):
        adjacency[edge["source"]].append(edge)
    registrations_by_name: dict[str, list[dict]] = defaultdict(list)
    for item in (pipeline.get("python_pipeline") or {}).get(
        "custom_action_registrations", []
    ):
        registrations_by_name[item["name"]].append(item)

    flows: list[dict] = []
    for task in tasks[:max_tasks]:
        entry = task.get("entry") or ""
        node_order: list[str] = []
        seen_nodes: set[str] = set()
        selected_edges: list[dict] = []
        selected_edge_keys: set[tuple[str, str, str, tuple[str, ...]]] = set()
        truncated = False

        def add_node(name: str) -> None:
            if name and name not in seen_nodes:
                seen_nodes.add(name)
                node_order.append(name)

        add_node(entry)
        if entry in node_names:
            queue: deque[tuple[str, int]] = deque([(entry, 0)])
            expanded: set[str] = set()
            while queue:
                source, depth = queue.popleft()
                if source in expanded:
                    continue
                expanded.add(source)

                outgoing = adjacency.get(source, [])
                if depth >= max_depth:
                    if outgoing:
                        truncated = True
                    continue

                for edge in outgoing:
                    target = edge["target"]
                    attrs = tuple(edge.get("attrs") or ())
                    edge_key = (edge["source"], target, edge["field"], attrs)
                    if edge_key in selected_edge_keys:
                        continue
                    if len(selected_edges) >= max_edges:
                        truncated = True
                        break
                    if target not in seen_nodes and len(seen_nodes) >= max_nodes:
                        truncated = True
                        break

                    selected_edge_keys.add(edge_key)
                    selected_edges.append(
                        {
                            "source": edge["source"],
                            "target": target,
                            "field": edge["field"],
                            "attrs": list(attrs),
                        }
                    )
                    add_node(target)
                    if target in node_names and target not in expanded:
                        queue.append((target, depth + 1))

        custom_actions = []
        for item in pipeline.get("custom_action_nodes", []):
            if item["node"] not in seen_nodes:
                continue
            custom_actions.append(
                {
                    **item,
                    "registrations": registrations_by_name.get(
                        item["custom_action"], []
                    ),
                }
            )

        flows.append(
            {
                "task": task.get("name") or "<unnamed>",
                "entry": entry,
                "repeatable": task.get("repeatable", False),
                "entry_found": entry in node_names,
                "depth_limit": max_depth,
                "edge_limit": max_edges,
                "node_limit": max_nodes,
                "node_count": len(node_order),
                "edge_count": len(selected_edges),
                "truncated": truncated,
                "primary_path": trace_primary_path(entry, adjacency, node_names),
                "unresolved_refs": sorted(name for name in node_order if name in unresolved),
                "nodes": node_order,
                "edges": selected_edges,
                "custom_actions": custom_actions,
            }
        )
    return flows


def summarize_images(project_root: Path, resource_dirs: list[Path], image_files: list[Path]) -> dict:
    by_resource = Counter()
    by_dir = Counter()
    samples: list[dict] = []
    resource_lookup = {str(path.resolve()): path.name for path in resource_dirs}
    for path in image_files:
        resource_name = "<unknown>"
        image_rel = path.name
        for resource_dir in resource_dirs:
            try:
                sub = path.relative_to(resource_dir / "image")
            except ValueError:
                continue
            resource_name = resource_lookup[str(resource_dir.resolve())]
            image_rel = str(sub)
            parent = str(sub.parent) if str(sub.parent) != "." else "<root>"
            by_dir[f"{resource_name}/{parent}"] += 1
            break
        by_resource[resource_name] += 1
        if len(samples) < 40:
            samples.append({"resource": resource_name, "path": image_rel})

    return {
        "image_count": len(image_files),
        "by_resource": dict(by_resource.most_common()),
        "top_dirs": [
            {"dir": name, "count": count} for name, count in by_dir.most_common(40)
        ],
        "samples": samples,
    }


def analyze_project(project_root: str | Path) -> dict:
    root = Path(project_root).resolve()
    interface_path = find_interface(root)
    interface, interface_imports = load_interface_bundle(interface_path)

    resource_groups = resolve_resource_dirs(root, interface_path, interface)
    resource_dirs = unique_existing_resource_dirs(resource_groups)
    # 本地修改：同名节点的作用范围是资源目录（MaaFramework 按目录加载 bundle，同目录重名才被拒绝），
    # 不是 interface 的 resource 条目。按条目名映射时，一个条目列多个目录（如 PC = base + pc）
    # 会把 pc 对 base 的合法覆盖误报成同包重复；同一目录出现在多个条目里时还会被最后一个条目名覆盖。
    resource_bundle_for = {
        path: str(path)
        for group in resource_groups
        for path in group["existing_paths"]
    }
    pipeline_files, default_files = discover_pipeline_files(root, resource_dirs)
    image_files = discover_image_files(resource_dirs)
    pipeline = analyze_pipeline_files(
        root, pipeline_files, resource_dirs, resource_bundle_for
    )
    python_pipeline = analyze_python_pipeline_calls(root)

    controllers = [
        item.get("type") or item.get("name")
        for item in interface.get("controller", []) or []
        if isinstance(item, dict)
    ]
    tasks = [
        {
            "name": item.get("name") or "<unnamed>",
            "entry": item.get("entry") or "",
            "repeatable": item.get("repeatable", False),
        }
        for item in interface.get("task", []) or []
        if isinstance(item, dict)
    ]
    node_names = set(pipeline["node_names"])
    interface_entries = {task["entry"] for task in tasks if task["entry"]}
    python_targets = set(python_pipeline["targets"])
    externally_reached = interface_entries | python_targets
    pipeline["python_pipeline"] = python_pipeline
    pipeline["task_flow_graphs"] = build_task_flow_graphs(tasks, pipeline)
    pipeline["external_entry_nodes"] = sorted(externally_reached & node_names)
    pipeline["external_unresolved_refs"] = sorted(externally_reached - node_names)
    pipeline["orphan_candidates"] = sorted(
        set(pipeline["zero_in_degree_nodes"]) - externally_reached
    )

    return {
        "project_root": str(root),
        "project_name": interface.get("name") or root.name,
        "project_url": interface.get("github") or interface.get("url") or "",
        "interface_path": rel(interface_path, root) if interface_path else "",
        "interface_imports": interface_imports,
        "controllers": controllers,
        "agent": interface.get("agent") or {},
        "agent_scripts": analyze_agent_scripts(
            root, interface_path, interface.get("agent")
        ),
        "resource_groups": resource_groups,
        "tasks": tasks,
        "default_pipeline_files": [rel(p, root) for p in default_files],
        "pipeline_file_count": len(pipeline_files),
        "image_summary": summarize_images(root, resource_dirs, image_files),
        "pipeline": pipeline,
    }


def render_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_None detected._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(lines) + "\n"


def mermaid_label(value: Any, max_len: int = 64) -> str:
    text = display_value(value, max_len=max_len)
    return (
        text.replace("\\", "/")
        .replace('"', "'")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("|", "/")
    )


def flow_edge_label(edge: dict) -> str:
    label = edge.get("field") or "edge"
    attrs = edge.get("attrs") or []
    if attrs:
        label = f"{label} {','.join(attrs)}"
    return mermaid_label(label, max_len=40)


def render_task_flow_mermaid(flow: dict) -> str:
    if not flow.get("nodes"):
        return "_No graph nodes detected._\n"

    node_ids = {name: f"N{idx}" for idx, name in enumerate(flow["nodes"])}
    unresolved = set(flow.get("unresolved_refs") or [])
    lines = ["```mermaid", "flowchart TD"]
    for name in flow["nodes"]:
        suffix = " (?)" if name in unresolved else ""
        lines.append(f'    {node_ids[name]}["{mermaid_label(name + suffix)}"]')

    agent_ids: list[tuple[str, dict]] = []
    for idx, custom in enumerate(flow.get("custom_actions") or []):
        agent_id = f"A{idx}"
        registrations = custom.get("registrations") or []
        label_parts = [
            "Python Agent",
            f"CustomAction: {custom.get('custom_action') or '<unnamed>'}",
        ]
        if registrations:
            registration = registrations[0]
            label_parts.extend(
                [
                    registration.get("handler") or "<handler>",
                    f"{registration.get('file')}:{registration.get('line')}",
                ]
            )
        else:
            label_parts.append("registration not found")
        label = "<br/>".join(
            mermaid_label(part, max_len=80) for part in label_parts
        )
        lines.append(f'    {agent_id}["{label}"]')
        agent_ids.append((agent_id, custom))

    for edge in flow.get("edges", []):
        source_id = node_ids.get(edge["source"])
        target_id = node_ids.get(edge["target"])
        if not source_id or not target_id:
            continue
        label = flow_edge_label(edge)
        if edge.get("field") == "next":
            lines.append(f"    {source_id} -- {label} --> {target_id}")
        else:
            lines.append(f"    {source_id} -. {label} .-> {target_id}")

    for agent_id, custom in agent_ids:
        source_id = node_ids.get(custom.get("node"))
        if not source_id:
            continue
        lines.append(f"    {source_id} -. CustomAction call .-> {agent_id}")
        lines.append(f"    {agent_id} -. returns .-> {source_id}")

    if unresolved:
        lines.append("    classDef unresolved fill:#fff3cd,stroke:#b7791f,color:#1f2933")
        lines.append(
            "    class "
            + ",".join(node_ids[name] for name in flow["nodes"] if name in unresolved)
            + " unresolved"
        )

    if agent_ids:
        lines.append("    classDef agent fill:#e8f1ff,stroke:#2563eb,color:#172554")
        lines.append("    class " + ",".join(item[0] for item in agent_ids) + " agent")

    lines.append("```")
    return "\n".join(lines) + "\n"


def render_task_flow_sections(flows: list[dict], max_flows: int = FLOW_MAX_TASKS) -> str:
    if not flows:
        return "_None detected._\n"

    lines: list[str] = []
    for flow in flows[:max_flows]:
        task_name = flow.get("task") or "<unnamed>"
        entry = flow.get("entry") or "<missing>"
        path = " -> ".join(flow.get("primary_path") or []) or "None detected"
        custom_action_count = len(flow.get("custom_actions") or [])
        custom_action_suffix = (
            f"，另含 {custom_action_count} 个 Agent CustomAction"
            if custom_action_count
            else ""
        )
        lines.extend(
            [
                f"#### {task_name} (`{entry}`)",
                "",
                f"- 入口节点存在: {flow.get('entry_found')}",
                f"- 主路径: `{path}`",
                (
                    f"- 图规模: {flow.get('node_count', 0)} nodes / "
                    f"{flow.get('edge_count', 0)} edges"
                    + ("，已截断" if flow.get("truncated") else "")
                    + custom_action_suffix
                ),
                f"- 未解析引用: {', '.join(flow.get('unresolved_refs') or []) or 'None detected'}",
                "",
                render_task_flow_mermaid(flow),
            ]
        )
    return "\n".join(lines) + "\n"


def render_agent_script_paths_section(agent_scripts: dict, *, include_warnings: bool = True) -> str:
    """生成 Agent script paths 章节内容（含 Declared / Discovered / Cross-check）。"""
    if not agent_scripts.get("agent_block_present"):
        return "_No agent block / child_args detected._\n"

    declared = agent_scripts.get("declared") or []
    discovered = agent_scripts.get("discovered") or []

    declared_rows: list[list[Any]] = []
    for item in declared:
        arg = item.get("arg") or ""
        status = item.get("status") or "unknown"
        resolved = item.get("resolved") or "—"
        declared_rows.append([arg, status, resolved])

    discovered_rows: list[list[Any]] = []
    for item in discovered:
        candidate = item.get("candidate") or ""
        exists = "yes" if item.get("exists") else "no"
        discovered_rows.append([candidate, exists])

    lines: list[str] = [
        "**Declared (interface.json child_args)**",
        "",
        render_table(["Arg", "Status", "Resolved"], declared_rows),
        "**Discovered (root 与 4 层 ancestor，按约定入口名扫描)**",
        "",
        render_table(["Candidate", "Exists"], discovered_rows),
    ]

    unresolved_args = [
        item.get("arg")
        for item in declared
        if item.get("status") == "unresolved"
    ]
    orphan = agent_scripts.get("orphan_declarations") or []
    unused = agent_scripts.get("unused_candidates") or []

    lines.append("**Cross-check**")
    lines.append("")
    lines.append(
        "- Unresolved declarations: "
        + (", ".join(f"`{a}`" for a in unresolved_args) or "_(none)_")
    )
    lines.append(
        "- Discovered but unreferenced: "
        + (", ".join(f"`{p}`" for p in unused) or "_(none)_")
    )
    lines.append(
        "- Referenced but not in convention list: "
        + (", ".join(f"`{p}`" for p in orphan) or "_(none)_")
    )

    if include_warnings:
        warnings = agent_scripts.get("warnings") or []
        if warnings:
            lines.append("")
            lines.append("**Warnings**")
            lines.append("")
            for warning in warnings:
                lines.append(f"- {warning}")

    return "\n".join(lines) + "\n"


def render_summary(analysis: dict) -> str:
    pipeline = analysis["pipeline"]
    image_summary = analysis["image_summary"]
    lines = [
        f"# {analysis['project_name']} pipeline scan",
        "",
        f"- Root: `{analysis['project_root']}`",
        f"- Interface: `{analysis.get('interface_path') or 'not found'}`",
        f"- Interface imports: {len(analysis.get('interface_imports', {}).get('loaded', []))} loaded / "
        f"{len(analysis.get('interface_imports', {}).get('declared', []))} declared",
        f"- Controllers: {', '.join(analysis['controllers']) or 'unknown'}",
        f"- Resource groups: {len(analysis['resource_groups'])}",
        f"- Tasks: {len(analysis['tasks'])}",
        f"- Pipeline files: {analysis['pipeline_file_count']}",
        f"- Unique nodes: {pipeline['node_count']}",
        f"- Node definitions: {pipeline['node_definition_count']}",
        f"- Edges: {len(pipeline['edges'])} ({display_value(pipeline['edge_type_counts'])})",
        f"- Image files: {image_summary['image_count']}",
        "",
        "## Task Entries",
        render_table(
            ["Task", "Entry", "Repeatable"],
            [[t["name"], t["entry"], t["repeatable"]] for t in analysis["tasks"][:30]],
        ),
        "## Entry Flow Previews",
        render_table(
            ["Task", "Entry", "Found", "Primary Path", "Graph"],
            [
                [
                    flow["task"],
                    flow["entry"],
                    flow["entry_found"],
                    " -> ".join(flow.get("primary_path") or []),
                    f"{flow['node_count']} nodes / {flow['edge_count']} edges"
                    + ("; truncated" if flow.get("truncated") else ""),
                ]
                for flow in pipeline.get("task_flow_graphs", [])[:15]
            ],
        ),
        "## Python / Interface External Entries",
        render_table(
            ["Kind", "Target", "Calls", "Sample call site"],
            [
                [
                    item["kind"],
                    item["target"],
                    item["count"],
                    f"{item['callers'][0]} @ {item['locations'][0]}",
                ]
                for item in pipeline["python_pipeline"]["call_summaries"][:20]
            ],
        ),
        "## Agent Script Paths",
        render_agent_script_paths_section(
            analysis.get("agent_scripts") or {},
            include_warnings=False,
        ),
        "## Top Pipeline Files",
        render_table(
            ["File", "Nodes"],
            [[item["file"], item["node_count"]] for item in pipeline["file_summaries"][:20]],
        ),
        "## Common Nodes",
        render_table(
            ["Node", "In", "Out", "Recognition", "Action", "Categories"],
            [
                [
                    item["name"],
                    item["in_degree"],
                    item["out_degree"],
                    item["recognition"],
                    item["action"],
                    ", ".join(item["categories"]),
                ]
                for item in pipeline["common_nodes"][:25]
            ],
        ),
        "## Return / Exit Nodes",
        render_table(
            ["Node", "In", "Recognition", "Action", "Files"],
            [
                [
                    item["name"],
                    item["in_degree"],
                    item["recognition"],
                    item["action"],
                    ", ".join(item["files"]),
                ]
                for item in pipeline["return_exit_nodes"][:25]
            ],
        ),
        "## Image Directories",
        render_table(
            ["Directory", "Count"],
            [[item["dir"], item["count"]] for item in image_summary["top_dirs"][:25]],
        ),
        "## Risks",
        f"- Unresolved refs: {len(pipeline['unresolved_refs'])}",
        f"- Unresolved anchor refs: {len(pipeline['unresolved_anchor_refs'])}",
        f"- Dangling anchor targets: {len(pipeline['dangling_anchor_targets'])}",
        f"- Zero-in-degree nodes: {len(pipeline['zero_in_degree_nodes'])}",
        f"- Graph-isolated nodes: {len(pipeline['isolated_nodes'])}",
        f"- External entry nodes: {len(pipeline['external_entry_nodes'])}",
        f"- Zero-in-degree candidates after external-entry scan: {len(pipeline['orphan_candidates'])}",
        f"- Dynamic Python Pipeline calls: {len(pipeline['python_pipeline']['dynamic_calls'])}",
        f"- Duplicate node names: {len(pipeline['duplicate_nodes'])}",
        f"- Cross-bundle override node names: {len(pipeline['cross_bundle_override_nodes'])}",
        f"- Cycle candidates: {len(pipeline['cycle_candidates'])}",
        f"- Unresolved agent script paths: {analysis.get('agent_scripts', {}).get('declared_unresolved_count', 0)}",
        f"- Orphan agent script path declarations: {len(analysis.get('agent_scripts', {}).get('orphan_declarations', []))}",
        f"- Unreferenced agent entry candidates: {len(analysis.get('agent_scripts', {}).get('unused_candidates', []))}",
    ]
    for warning in analysis.get("interface_imports", {}).get("warnings", []):
        lines.append(f"- Interface import warning: {warning}")
    if pipeline["unresolved_refs"]:
        lines.append(f"- Unresolved sample: {', '.join(pipeline['unresolved_refs'][:20])}")
    if pipeline["unresolved_anchor_refs"]:
        lines.append(
            f"- Unresolved anchor refs sample: {', '.join(pipeline['unresolved_anchor_refs'][:20])}"
        )
    return "\n".join(lines) + "\n"


def render_basic_info(analysis: dict) -> str:
    pipeline = analysis["pipeline"]
    image_summary = analysis["image_summary"]
    resource_rows = [
        [group["name"], ", ".join(group["raw_paths"]), len(group["existing_paths"])]
        for group in analysis["resource_groups"]
    ]
    task_rows = [[task["name"], task["entry"], task["repeatable"]] for task in analysis["tasks"]]
    common_rows = [
        [
            item["name"],
            item["in_degree"],
            item["recognition"],
            item["action"],
            ", ".join(item["categories"]),
        ]
        for item in pipeline["common_nodes"][:30]
    ]
    return_rows = [
        [
            item["name"],
            item["in_degree"],
            item["recognition"],
            item["action"],
            ", ".join(item["files"]),
        ]
        for item in pipeline["return_exit_nodes"][:30]
    ]
    ocr_rows = [
        [item["node"], item["expected"], item["roi"], item["file"]]
        for item in pipeline["ocr_expected"][:40]
    ]
    template_rows = [
        [item["node"], item["template"], item["roi"], item["file"]]
        for item in pipeline["templates"][:40]
    ]
    roi_rows = [
        [item["node"], item["roi"], item["file"]]
        for item in pipeline["roi_nodes"][:30]
    ]
    python_call_rows = [
        [
            item["kind"],
            item["target"],
            item["count"],
            f"{item['callers'][0]} @ {item['locations'][0]}",
        ]
        for item in pipeline["python_pipeline"]["call_summaries"][:25]
    ]

    lines = [
        "# Basic Info",
        "",
            "> Auto-generated by `maa-project-init`. Review TODO items before relying on this file for automation edits.",
        "",
        "## 0. Maa Skills 接力协议",
        "",
        "`basic_info.md` 是项目上下文缓存，不是 Pipeline 源文件。Maa skills 应先读本节，再按任务读取相关章节，并始终以当前源码和实时设备结果为准。",
        "",
        render_table(
            ["Consumer skill", "Read first", "Must re-verify"],
            [
            ["maa-pipeline-guide / maa-pipeline-generate", "3, 4, 5, 7, 8, 9", "待修改节点及目标页面"],
            ["maa-pipeline-graph", "2, 3, 6", "跨文件边、Python 调用和 interface entry"],
            ["maa-pipeline-option", "1, 2, 6", "pipeline_override 与 Python 读取路径"],
            ["maa-pipeline-testing", "2, 5, 7, 8, 9, 10", "当前截图、OCR 命中和高风险动作"],
            ],
        ),
            "- 若本文件缺失，且当前会话可用 `maa-project-init`，先运行它。",
        "- 若本文件早于相关 Pipeline / interface / agent 文件，视为可能过期；先重跑摘要，不要静默覆盖本文件。",
        "- 发现源码或实机结果与本文冲突时，记录冲突并采用新证据；未经确认不要覆盖已有非空文件。",
        "",
        "## 1. 项目概览",
        "",
        f"- 项目名: `{analysis['project_name']}`",
        f"- 根目录: `{analysis['project_root']}`",
        f"- 仓库/主页: {analysis.get('project_url') or 'TODO'}",
        f"- interface: `{analysis.get('interface_path') or 'TODO'}`",
        f"- 控制器: {', '.join(analysis['controllers']) or 'TODO'}",
        f"- Agent: `{display_value(analysis.get('agent')) or 'TODO'}`",
        "",
        "## 2. 资源组与入口任务",
        "",
        "### Resource groups",
        render_table(["Name", "Paths", "Existing paths"], resource_rows),
        "### Agent script paths",
        "",
        "> 接口声明的 agent 入口文件静态扫描结果；只读、不修改 interface.json。",
        "",
        render_agent_script_paths_section(analysis.get("agent_scripts") or {}),
        "### Task entries",
        render_table(["Task", "Entry", "Repeatable"], task_rows),
        "## 3. 主要 Pipeline",
        "",
        f"- Pipeline 文件数: {analysis['pipeline_file_count']}",
        f"- 默认配置文件: {', '.join(analysis['default_pipeline_files']) or 'None detected'}",
        f"- 唯一节点数: {pipeline['node_count']}",
        f"- 节点定义数: {pipeline['node_definition_count']}",
        "",
        render_table(
            ["File", "Nodes"],
            [[item["file"], item["node_count"]] for item in pipeline["file_summaries"][:30]],
        ),
        "### 入口主链路流程图",
        "",
        "从 `interface.json` 的 task entry 出发，按 `next/on_error/interrupt` 展开有限深度流程图；公共返回、确认、退出节点会自然出现在图中。",
        "",
        render_task_flow_sections(pipeline.get("task_flow_graphs", [])),
        "## 4. 公共基础节点",
        "",
        "这些节点通常被多条链路引用，或名称/行为显示它们是通用 UI 控制节点。",
        "",
        render_table(["Node", "In", "Recognition", "Action", "Categories"], common_rows),
        "## 5. 返回 / 退出 / 弹窗处理",
        "",
        "重点关注返回、退出、关闭、确认、重连、体力不足等流程。MaaGumballs 一类项目常通过 `[JumpBack]` 把这些节点挂到主链路中。",
        "",
        render_table(["Node", "In", "Recognition", "Action", "Files"], return_rows),
        "### Confirm / Cancel nodes",
        render_table(
            ["Node", "In", "Recognition", "Action"],
            [
                [item["name"], item["in_degree"], item["recognition"], item["action"]]
                for item in pipeline["confirm_nodes"][:20]
            ],
        ),
        "## 6. 节点关系摘要",
        "",
        f"- 边数量: {len(pipeline['edges'])}",
        f"- 边类型: `{display_value(pipeline['edge_type_counts'])}`",
        f"- 跨文件引用数: {len(pipeline['cross_file_edges'])}",
        f"- 未解析引用数: {len(pipeline['unresolved_refs'])}",
        f"- 未声明 anchor 引用数: {len(pipeline['unresolved_anchor_refs'])}",
        f"- anchor 目标缺失数: {len(pipeline['dangling_anchor_targets'])}",
        f"- 孤立节点数: {len(pipeline['isolated_nodes'])}",
        f"- 重复节点名数: {len(pipeline['duplicate_nodes'])}",
        f"- 跨 bundle 覆盖节点名数: {len(pipeline['cross_bundle_override_nodes'])}",
        f"- 疑似循环/SCC 数: {len(pipeline['cycle_candidates'])}",
        "",
        "### Top in-degree nodes",
        render_table(
            ["Node", "In"],
            [[item["name"], item["in_degree"]] for item in pipeline["top_in_degree"][:25]],
        ),
        "### Python / interface 外部入口",
        "",
        f"- 扫描 Python 文件数: {pipeline['python_pipeline']['python_file_count']}",
        f"- 静态 Python Pipeline 调用数: {len(pipeline['python_pipeline']['calls'])}",
        f"- 动态目标调用数: {len(pipeline['python_pipeline']['dynamic_calls'])}",
        f"- 外部入口命中节点数: {len(pipeline['external_entry_nodes'])}",
        f"- 排除外部入口后的无入边候选数: {len(pipeline['orphan_candidates'])}",
        "",
        render_table(
            ["Kind", "Target", "Calls", "Sample call site"],
            python_call_rows,
        ),
        "## 7. OCR 文字识别约定",
        "",
        "以下为扫描到的 `expected` 样例。请人工补充 OCR 易错字、替换规则和跨语言差异。",
        "",
        render_table(["Node", "Expected", "ROI", "File"], ocr_rows),
        "## 8. TemplateMatch 图片模板",
        "",
        f"- 图片文件数: {image_summary['image_count']}",
        "",
        "### Image directories",
        render_table(
            ["Directory", "Count"],
            [[item["dir"], item["count"]] for item in image_summary["top_dirs"][:40]],
        ),
        "### TemplateMatch usage samples",
        render_table(["Node", "Template", "ROI", "File"], template_rows),
        "## 9. 分辨率与 ROI 约定",
        "",
        "- MaaMCP ADB 默认按截图短边 720 归一化；横屏/竖屏通过截图宽高判断。",
        "- Pipeline 中的 ROI 应结合项目实际截图基准确认，不要盲目从其他设备复制。",
        "- TODO: 用一次实机 `screencap` 记录目标设备分辨率、方向、DPI 和常用页面 ROI。",
        "",
        render_table(["Node", "ROI", "File"], roi_rows),
        "### MaaMCP 实机验证记录",
        "",
        "- 当前状态: `static-scan-only`（生成器不会把未验证的截图猜测写成事实）。",
        "- 记录格式: 时间、controller/device、截图尺寸与方向、页面证据、测试节点、score、是否执行动作。",
        "- 同一次验证的 OCR 与截图必须确认来自稳定的同一页面；画面切换时分别记录，不能合并为永久事实。",
        "- TODO: 使用 `DoNothing` 探针验证至少一个公共返回/确认节点，并保留 TaskDetail 结果。",
        "",
        "## 10. 风险清单与待确认项",
        "",
        f"- 未解析引用: {', '.join(pipeline['unresolved_refs'][:30]) or 'None detected'}",
        f"- 未声明的 anchor 引用: {', '.join(pipeline['unresolved_anchor_refs'][:30]) or 'None detected'}",
        (
            "- 目标节点缺失的 anchor 声明样例: "
            + (display_value(pipeline['dangling_anchor_targets'][:10]) or 'None detected')
        ),
        f"- 图内孤立节点样例: {', '.join(pipeline['isolated_nodes'][:30]) or 'None detected'}",
        f"- 排除外部入口后的无入边候选: {', '.join(pipeline['orphan_candidates'][:30]) or 'None detected'}",
        f"- Python 动态目标调用数: {len(pipeline['python_pipeline']['dynamic_calls'])}",
        f"- 外部入口未解析引用: {', '.join(pipeline['external_unresolved_refs'][:30]) or 'None detected'}",
        f"- 重复节点名样例: {', '.join(pipeline['duplicate_nodes'][:30]) or 'None detected'}",
        f"- 跨 bundle 覆盖样例: {', '.join(pipeline['cross_bundle_override_nodes'][:30]) or 'None detected'}",
        f"- 疑似循环样例: {display_value(pipeline['cycle_candidates'][:10]) or 'None detected'}",
        f"- Agent script paths unresolved: {analysis.get('agent_scripts', {}).get('declared_unresolved_count', 0)}",
        f"- Agent script path orphan declarations: {len(analysis.get('agent_scripts', {}).get('orphan_declarations', []))}",
        f"- Agent script path unreferenced candidates: {len(analysis.get('agent_scripts', {}).get('unused_candidates', []))}",
        "- TODO: 人工确认哪些高入度节点是真公共节点，哪些只是历史遗留或渠道覆盖。",
        "- TODO: 对关键入口任务跑一次 MaaMCP 实机截图/OCR，补充游戏首页、弹窗、返回路径。",
        "",
    ]
    return "\n".join(lines)


def write_basic_info(analysis: dict, overwrite: bool = False) -> Path:
    path = Path(analysis["project_root"]) / "basic_info.md"
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} already exists; pass --overwrite only after explicit confirmation"
        )
    path.write_text(render_basic_info(analysis), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    # Pipeline node names in consumer projects are frequently non-ASCII;
    # avoid UnicodeEncodeError on consoles defaulting to cp1252/cp936 (Windows).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", nargs="?", default=".", help="MaaFramework consumer project root")
    parser.add_argument("--json", action="store_true", help="print machine-readable analysis JSON")
    parser.add_argument("--write-basic-info", action="store_true", help="write <project_root>/basic_info.md")
    parser.add_argument("--overwrite", action="store_true", help="allow overwriting an existing basic_info.md")
    args = parser.parse_args(argv)

    analysis = analyze_project(args.project_root)
    if args.json:
        print(json.dumps(analysis, ensure_ascii=False, indent=2))
    else:
        print(render_summary(analysis))

    if args.write_basic_info:
        try:
            path = write_basic_info(analysis, overwrite=args.overwrite)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
