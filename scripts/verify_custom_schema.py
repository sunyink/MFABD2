"""Custom 冷检查：不跑游戏就能查出的自定义识别 / 动作配置错误。

只报写错必出事的四类，可省略的参数不填不算错：
  1. 引用了 agent/ 里没注册的名字 —— 运行时找不到，节点必失败；
  2. 参数与 deps/tools/custom.*.schema.json 的声明不符 —— 必填缺失、类型或取值代码不接受、键名拼错；
  3. 参数里指向的节点不存在；
  4. schema 与注册名对不上 —— 新增、改名、删除 Custom 后没同步 schema。

只写了参数、没写名字的节点（覆盖包、interface 的 pipeline_override）按同名节点的 Custom 检查，
且不查必填：那里的参数可能只是一部分。Python 运行时注入的参数不在检查范围内。

    python scripts/verify_custom_schema.py              # 有问题则退出码 1
    python scripts/verify_custom_schema.py --self-test  # 校验器自检
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KINDS = ("action", "recognition")
ANNOTATIONS = {"description", "markdownDescription", "title", "default", "examples", "$comment", "deprecated"}
# 本脚本会执行的约束关键字。schema 里出现表外的关键字视为错误：写了却没人检查，比不写更糟。
SUPPORTED = {
    "type", "enum", "const", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
    "minProperties", "anyOf", "oneOf", "allOf", "not", "$ref", "x-node-ref", "x-node-ref-keys",
}
PLACEHOLDER = re.compile(r"\{[^{}]+\}")
ANCHOR_PREFIX = "[Anchor]"
JSON_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}
TYPE_LABELS = {"object": "对象", "array": "数组", "string": "字符串", "boolean": "布尔值",
               "null": "null", "integer": "整数", "number": "数字"}


def load_jsonc(path: Path):
    """读带注释的 JSON。字符串里的 // 不算注释。"""
    text = path.read_text(encoding="utf-8-sig")
    out, i, size, in_string = [], 0, len(text), False
    while i < size:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < size:
                out.append(text[i + 1])
                i += 2
                continue
            in_string = char != '"'
        elif char == '"':
            in_string = True
            out.append(char)
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = size if end < 0 else end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            stop = size if end < 0 else end + 2
            out.append("\n" * text.count("\n", i, stop))
            i = stop
            continue
        else:
            out.append(char)
        i += 1
    clean = "".join(out)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", clean))


def same(a, b) -> bool:
    """JSON 语义的相等：True 不等于 1。"""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def describe_type(value) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if JSON_TYPES[name](value):
            return TYPE_LABELS[name]
    return type(value).__name__


def resolve_ref(root_schema: dict, ref: str):
    if not ref.startswith("#/"):
        raise ValueError(f"只支持文件内引用，遇到 {ref!r}")
    node = root_schema
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def required_only(schema) -> bool:
    return isinstance(schema, dict) and set(schema) - ANNOTATIONS == {"required"}


class Checker:
    """按 SUPPORTED 里的关键字校验一个值，并收集其中标注为节点引用的字符串。"""

    def __init__(self, root_schema: dict, *, partial: bool = False, placeholders: bool = False):
        self.root = root_schema
        self.partial = partial  # 参数可能只是一部分：不查必填，互斥写法按「满足其一」处理
        self.placeholders = placeholders  # interface 的输入项占位符 {xxx} 运行时才有值，跳过
        self.refs: list[tuple[str, str, object]] = []

    def check(self, schema: dict, value, where: str) -> list[str]:
        if self.placeholders and isinstance(value, str) and PLACEHOLDER.search(value):
            return []
        errors: list[str] = []
        if "$ref" in schema:
            errors += self.check(resolve_ref(self.root, schema["$ref"]), value, where)
        wanted = schema.get("type")
        if wanted is not None:
            options = [wanted] if isinstance(wanted, str) else list(wanted)
            if not any(JSON_TYPES[name](value) for name in options):
                label = "或".join(TYPE_LABELS[name] for name in options)
                return errors + [f"{where} 应为{label}，实际是{describe_type(value)}"]
        if "const" in schema and not same(value, schema["const"]):
            errors.append(f"{where} 只能是 {json.dumps(schema['const'], ensure_ascii=False)}")
        if "enum" in schema and not any(same(value, item) for item in schema["enum"]):
            allowed = "、".join(json.dumps(item, ensure_ascii=False) for item in schema["enum"])
            errors.append(f"{where} 的取值 {json.dumps(value, ensure_ascii=False)} 不被接受，可选 {allowed}")
        if isinstance(value, str):
            errors += self._string(schema, value, where)
        elif JSON_TYPES["number"](value):
            errors += self._number(schema, value, where)
        elif isinstance(value, list):
            errors += self._array(schema, value, where)
        elif isinstance(value, dict):
            errors += self._object(schema, value, where)
        for sub in schema.get("allOf", []):
            errors += self.check(sub, value, where)
        if "anyOf" in schema:
            errors += self._branches(schema["anyOf"], value, where, exclusive=False)
        if "oneOf" in schema:
            errors += self._branches(schema["oneOf"], value, where, exclusive=not self.partial)
        if "not" in schema and not self.partial and not self._quiet(schema["not"], value, where)[0]:
            errors.append(f"{where} 的取值 {json.dumps(value, ensure_ascii=False)} 不被接受")
        return errors

    def _string(self, schema, value, where):
        errors = []
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where} 不能为空" if schema["minLength"] == 1 else f"{where} 至少 {schema['minLength']} 个字符")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where} 最多 {schema['maxLength']} 个字符")
        if schema.get("x-node-ref"):
            self.refs.append((value, where, schema["x-node-ref"]))
        return errors

    def _number(self, schema, value, where):
        errors = []
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where} 不能小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where} 不能大于 {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{where} 必须大于 {schema['exclusiveMinimum']}")
        return errors

    def _array(self, schema, value, where):
        errors = []
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where} 至少要有 {schema['minItems']} 项，实际 {len(value)} 项")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where} 最多 {schema['maxItems']} 项，实际 {len(value)} 项")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors += self.check(schema["items"], item, f"{where}[{index}]")
        return errors

    def _object(self, schema, value, where):
        errors = []
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                errors += self.check(sub, value[key], f"{where}.{key}")
        if not self.partial:
            errors += [f"{where} 缺少必填的 {key}" for key in schema.get("required", []) if key not in value]
            if len(value) < schema.get("minProperties", 0):
                errors.append(f"{where} 不能是空对象")
        extra = schema.get("additionalProperties")
        for key in value:
            if key in properties:
                continue
            if extra is False:
                close = difflib.get_close_matches(key, properties, n=1)
                hint = f"，是不是想写 {close[0]}" if close else ""
                errors.append(f"{where} 里的键 {key!r} 没有这个参数{hint}（可用：{'、'.join(properties)}）")
            elif isinstance(extra, dict):
                errors += self.check(extra, value[key], f"{where}.{key}")
        if schema.get("x-node-ref-keys"):
            self.refs += [(key, f"{where} 的键 {key!r}", schema["x-node-ref-keys"]) for key in value]
        return errors

    def _quiet(self, schema, value, where):
        """试校验一个分支：返回 (错误, 该分支收集到的节点引用)，不污染 self.refs。"""
        kept, self.refs = self.refs, []
        errors = self.check(schema, value, where)
        found, self.refs = self.refs, kept
        return errors, found

    def _branches(self, branches, value, where, *, exclusive: bool):
        results = [self._quiet(branch, value, where) for branch in branches]
        passed = [index for index, (errors, _) in enumerate(results) if not errors]
        if not passed:
            if all(required_only(branch) for branch in branches):
                # <键>_code 是「由代码注入」的标记写法，提示里不提，免得把人带偏
                shown = [branch["required"] for branch in branches
                         if not all(key.endswith("_code") for key in branch["required"])]
                if len(shown) == 1:
                    return [f"{where} 缺少必填的 {' 与 '.join(shown[0])}"]
                return [f"{where} 缺少必填项：需要 {' 或 '.join(' + '.join(keys) for keys in shown)}"]
            best = min((errors for errors, _ in results), key=len)
            return [f"{where} 不符合任何一种允许的写法；最接近的一种还差：{'；'.join(best)}"]
        if exclusive and len(passed) > 1:
            if all(required_only(branch) for branch in branches):
                names = " 与 ".join(" + ".join(branches[index]["required"]) for index in passed)
                return [f"{where} 里 {names} 不能同时出现"]
            return [f"{where} 同时符合了多种互斥的写法"]
        self.refs += results[passed[0]][1]
        return []


def unknown_keywords(schema, where="") -> list[str]:
    found = []
    if not isinstance(schema, dict):
        return found
    for key, value in schema.items():
        if key in ANNOTATIONS:
            continue
        if key not in SUPPORTED:
            found.append(f"{where or '(顶层)'} 用了本脚本不会检查的关键字 {key!r}")
        elif key == "properties":
            for name, sub in value.items():
                found += unknown_keywords(sub, f"{where}.{name}")
        elif key in ("items", "additionalProperties", "not"):
            found += unknown_keywords(value, where)
        elif key in ("anyOf", "oneOf", "allOf"):
            for sub in value:
                found += unknown_keywords(sub, where)
    return found


def schema_names(kind: str, schema: dict) -> tuple[dict, list[str]]:
    """返回 {名字: 定义} 与 schema 自身结构上的问题。"""
    key, problems, by_name = f"custom_{kind}", [], {}
    enum = schema.get("properties", {}).get(key, {}).get("enum", [])
    referenced = [item.get("$ref", "").rsplit("/", 1)[-1] for item in schema.get("anyOf", [])]
    for def_name, definition in schema.get("$defs", {}).items():
        problems += unknown_keywords(definition, def_name)
        const = definition.get("properties", {}).get(key, {}).get("const") if isinstance(definition, dict) else None
        if const is None:
            continue
        if const != def_name:
            problems.append(f"定义 {def_name} 里写的名字是 {const!r}，两者必须一致")
        by_name[const] = definition
    for label, names in (("properties.%s.enum" % key, enum), ("anyOf", referenced)):
        doubled = sorted({name for name in names if names.count(name) > 1})
        if doubled:
            problems.append(f"{label} 里重复：{'、'.join(doubled)}")
        missing = sorted(set(by_name) - set(names))
        extra = sorted(set(names) - set(by_name))
        if missing:
            problems.append(f"{label} 里漏了：{'、'.join(missing)}")
        if extra:
            problems.append(f"{label} 里多了没有定义的：{'、'.join(extra)}")
    return by_name, problems


def registered_names(root: Path) -> tuple[dict, list[str]]:
    names: dict = {kind: {} for kind in KINDS}
    problems = []
    for path in sorted((root / "agent").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as error:
            problems.append(f"{relative} 解析失败：{error}")
            continue
        for node in ast.walk(tree):
            for decorator in getattr(node, "decorator_list", []):
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in ("custom_action", "custom_recognition")):
                    continue
                kind = decorator.func.attr[len("custom_"):]
                first = decorator.args[0] if decorator.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names[kind].setdefault(first.value, []).append(f"{relative}:{decorator.lineno}")
                else:
                    problems.append(f"{relative}:{decorator.lineno} 的注册名不是字符串字面量，无法静态核对")
    for kind in KINDS:
        for name, places in names[kind].items():
            if len(places) > 1:
                problems.append(f"custom_{kind} {name!r} 注册了多次：{'、'.join(places)}")
    return names, problems


def line_of(path: Path, node_name: str):
    pattern = re.compile(r'^\s*"%s"\s*:' % re.escape(node_name))
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if pattern.match(line):
                return number
    except OSError:
        pass
    return None


class Audit:
    def __init__(self, root: Path):
        self.root = root
        self.problems: list[tuple[str, int | None, str]] = []  # (文件, 行, 说明)
        self.stats = {"full": 0, "partial": 0, "refs": 0}
        self.schemas, self.definitions = {}, {}
        for kind in KINDS:
            path = root / "deps" / "tools" / f"custom.{kind}.schema.json"
            relative = path.relative_to(root).as_posix()
            try:
                self.schemas[kind] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                self.schemas[kind], self.definitions[kind] = {}, {}
                self.problems.append((relative, None, f"读不了：{error}"))
                continue
            self.definitions[kind], issues = schema_names(kind, self.schemas[kind])
            self.problems += [(relative, None, issue) for issue in issues]
        self.registered, issues = registered_names(root)
        self.problems += [("agent/", None, issue) for issue in issues]
        self.packs: dict = {}  # 包名 -> {节点名: (文件, 节点)}
        self.interface = None
        self.pending: list[tuple] = []  # 待核对的节点引用，等所有用法过完再查
        # 运行时才出现的节点：schema 顶层 x-dynamic-nodes 列出的，加上带 create 的 PatchPipeline 新建的
        self.dynamic = {name for schema in self.schemas.values() for name in schema.get("x-dynamic-nodes", [])}

    def load(self):
        for pack_dir in sorted((self.root / "assets" / "resource").glob("*/pipeline")):
            nodes = self.packs.setdefault(pack_dir.parent.name, {})
            for path in sorted(pack_dir.rglob("*.json")):
                if path.name.startswith("."):
                    continue  # 编辑器的布局文件，不是 Pipeline
                try:
                    data = load_jsonc(path)
                except ValueError as error:
                    self.problems.append((self._rel(path), None, f"JSON 解析失败：{error}"))
                    continue
                if isinstance(data, dict):
                    for name, node in data.items():
                        if isinstance(node, dict) and not name.startswith("$"):
                            nodes[name] = (path, node)
        path = self.root / "assets" / "interface.json"
        if path.is_file():
            try:
                self.interface = load_jsonc(path)
            except ValueError as error:
                self.problems.append((self._rel(path), None, f"JSON 解析失败：{error}"))

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _owner(self, node_name: str, kind: str):
        """同名节点在别的包里用的 Custom 名字；base 优先。"""
        key = f"custom_{kind}"
        for pack in sorted(self.packs, key=lambda name: (name != "base", name)):
            entry = self.packs[pack].get(node_name)
            if entry and isinstance(entry[1].get(key), str):
                return entry[1][key]
        return None

    def run(self) -> None:
        self.load()
        known = {name for nodes in self.packs.values() for name in nodes}
        overrides = list(self._overrides(self.interface, "")) if self.interface is not None else []
        known |= {name for _, mapping in overrides for name in mapping}
        self._sync()
        for pack in sorted(self.packs):
            for node_name, (path, node) in self.packs[pack].items():
                place = (self._rel(path), line_of(path, node_name))
                self._names(node, place, node_name)
                for kind in KINDS:
                    for holder in self._holders(node, f"custom_{kind}"):
                        self._site(kind, holder[f"custom_{kind}"], holder, place, node_name, partial=False)
                    if f"custom_{kind}_param" in node and f"custom_{kind}" not in node:
                        owner = self._owner(node_name, kind)
                        if owner:
                            self._site(kind, owner, node, place, node_name, partial=True)
        for trail, mapping in overrides:
            for node_name, override in mapping.items():
                if not isinstance(override, dict):
                    continue
                place = ("assets/interface.json", None)
                label = f"{trail} › {node_name}"
                self._names(override, place, label)
                for kind in KINDS:
                    if f"custom_{kind}_param" not in override:
                        continue
                    owner = override.get(f"custom_{kind}")
                    owner = owner if isinstance(owner, str) else self._owner(node_name, kind)
                    if owner:
                        self._site(kind, owner, override, place, label, partial=True, placeholders=True)
        known |= self.dynamic
        for place, label, value, where in self.pending:
            if value not in known:
                close = difflib.get_close_matches(value, known, n=1)
                hint = f"，是不是想写 {close[0]}" if close else ""
                target = where if where.endswith(repr(value)) else f"{where} 指向的 {value!r}"
                self.problems.append((*place, f"{label}：{target} 不是已存在的节点{hint}"))

    def _overrides(self, value, trail):
        """interface.json 里所有 pipeline_override：产出 (位置描述, {节点名: 覆盖})。"""
        if isinstance(value, dict):
            for key, sub in value.items():
                here = f"{trail}.{key}" if trail else key
                if key == "pipeline_override" and isinstance(sub, dict):
                    yield trail or "(顶层)", sub
                else:
                    yield from self._overrides(sub, here)
        elif isinstance(value, list):
            for index, sub in enumerate(value):
                label = sub.get("name") if isinstance(sub, dict) and isinstance(sub.get("name"), str) else index
                yield from self._overrides(sub, f"{trail}[{label}]")

    def _holders(self, value, key):
        """节点里所有写了 Custom 名字的对象（含 V2 嵌套与 And / Or 的内联子识别）；不进参数内部。"""
        if isinstance(value, dict):
            if isinstance(value.get(key), str):
                yield value
            for name, sub in value.items():
                if not name.endswith("_param"):
                    yield from self._holders(sub, key)
        elif isinstance(value, list):
            for sub in value:
                yield from self._holders(sub, key)

    def _names(self, value, place, label):
        """任何层级出现的 Custom 名字都必须注册过（含 PatchPipeline 补丁里写的）。"""
        if isinstance(value, dict):
            for kind in KINDS:
                name = value.get(f"custom_{kind}")
                if isinstance(name, str) and name not in self.registered[kind] and not PLACEHOLDER.search(name):
                    close = difflib.get_close_matches(name, self.registered[kind], n=1)
                    hint = f"，是不是想写 {close[0]}" if close else ""
                    self.problems.append((*place, f"{label}：custom_{kind} {name!r} 没有在 agent/ 里注册{hint}"))
            for sub in value.values():
                self._names(sub, place, label)
        elif isinstance(value, list):
            for sub in value:
                self._names(sub, place, label)

    def _sync(self):
        for kind in KINDS:
            relative = f"deps/tools/custom.{kind}.schema.json"
            if not self.schemas[kind]:
                continue
            code, declared = set(self.registered[kind]), set(self.definitions[kind])
            for name in sorted(code - declared):
                where = self.registered[kind][name][0]
                self.problems.append((relative, None, f"custom_{kind} {name!r}（{where}）已注册但 schema 里没有定义，请补一条"))
            for name in sorted(declared - code):
                self.problems.append((relative, None, f"schema 里的 custom_{kind} {name!r} 在 agent/ 里找不到注册，请删掉或改名"))

    def _site(self, kind, name, holder, place, label, *, partial, placeholders=False):
        definition = self.definitions[kind].get(name)
        if definition is None:
            return  # 没注册或 schema 没定义，已在别处报过
        key, param_key = f"custom_{kind}", f"custom_{kind}_param"
        instance = {key: name}
        for field in (param_key, param_key + "_code"):
            if holder.get(field) is not None:
                instance[field] = holder[field]
        checker = Checker(self.schemas[kind], partial=partial, placeholders=placeholders)
        errors = checker.check(definition, instance, name)
        self.stats["partial" if partial else "full"] += 1
        root_param = instance.get(param_key)
        prefix = name + "."
        for value, where, rule in checker.refs:
            if value.startswith(ANCHOR_PREFIX) or PLACEHOLDER.search(value):
                continue
            unless = rule.get("unless") if isinstance(rule, dict) else None
            if unless and isinstance(root_param, dict) and root_param.get(unless):
                self.dynamic.add(value)
                continue
            self.stats["refs"] += 1
            self.pending.append((place, label, value, where[len(prefix):] if where.startswith(prefix) else where))
        for error in errors:
            self.problems.append((*place, f"{label}：{error[len(prefix):] if error.startswith(prefix) else error}"))

    def report(self) -> int:
        stats = self.stats
        print(f"注册：动作 {len(self.registered['action'])} 个、识别 {len(self.registered['recognition'])} 个；"
              f"检查了 {stats['full']} 处完整用法、{stats['partial']} 处只写参数的覆盖、{stats['refs']} 个节点引用")
        if not self.problems:
            print("✅ Custom 冷检查通过")
            return 0
        in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        for file, line, message in self.problems:
            location = f"{file}:{line}" if line else file
            print(f"❌ {location}  {message}")
            if in_actions:
                position = f"file={file}" + (f",line={line}" if line else "")
                print(f"::error {position},title=Custom 冷检查::{message}")
        print(f"\n共 {len(self.problems)} 处问题。参数声明在 deps/tools/custom.action.schema.json 与 "
              f"custom.recognition.schema.json；如果是代码新增了参数或取值，请同步改那里。")
        return 1


def self_test() -> int:
    schema = {
        "$defs": {
            "Demo": {
                "properties": {
                    "custom_action": {"const": "Demo"},
                    "custom_action_param": {
                        "type": "object",
                        "properties": {
                            "node": {"type": "string", "x-node-ref": {"unless": "create"}},
                            "mode": {"type": "string", "enum": ["a", "b"]},
                            "count": {"type": "integer", "minimum": 1},
                            "create": {"type": "boolean"},
                            "target": {"type": "string"},
                            "patch": {"type": "object"},
                            "patches": {"type": "object"},
                        },
                        "required": ["node"],
                        "additionalProperties": False,
                        "oneOf": [{"required": ["target", "patch"]}, {"required": ["patches"]}],
                    },
                },
            },
        },
    }
    definition = schema["$defs"]["Demo"]

    def run(param, **options):
        checker = Checker(schema, **options)
        return checker.check(definition, {"custom_action": "Demo", "custom_action_param": param}, "Demo"), checker.refs

    good = {"node": "N", "patches": {}}
    cases = [
        ("合法参数", run(good)[0], 0),
        ("缺必填", run({"patches": {}})[0], 1),
        ("键名拼错", run({**good, "nod": 1})[0], 1),
        ("类型不符", run({**good, "count": "3"})[0], 1),
        ("布尔不算整数", run({**good, "count": True})[0], 1),
        ("取值不在枚举内", run({**good, "mode": "c"})[0], 1),
        ("低于下限", run({**good, "count": 0})[0], 1),
        ("互斥写法同时出现", run({"node": "N", "patches": {}, "target": "x", "patch": {}})[0], 1),
        ("两种写法都没写", run({"node": "N"})[0], 1),
        ("只写了一部分：不查必填", run({"mode": "a"}, partial=True)[0], 0),
        ("只写了一部分：键名拼错照查", run({"mdoe": "a"}, partial=True)[0], 1),
        ("占位符跳过", run({"count": "{次数}"}, partial=True, placeholders=True)[0], 0),
        ("参数不是对象", run("N")[0], 1),
    ]
    failed = [f"{label}：期望 {expected} 条，实得 {errors}" for label, errors, expected in cases if len(errors) != expected]
    if [value for value, _, _ in run(good)[1]] != ["N"]:
        failed.append("节点引用没有被收集")
    if unknown_keywords({"properties": {"x": {"pattern": "^a"}}}) == []:
        failed.append("没有拦下不认识的关键字")
    sample = '{\n  // 注释\n  "url": "https://example.com", /* 块\n注释 */ "n": [1, 2,],\n}'
    path = Path(__file__).with_name("__verify_custom_schema_selftest__.json")
    try:
        path.write_text(sample, encoding="utf-8")
        if load_jsonc(path) != {"url": "https://example.com", "n": [1, 2]}:
            failed.append("带注释的 JSON 读错了")
    finally:
        path.unlink(missing_ok=True)
    for line in failed:
        print(f"❌ 自检失败  {line}")
    print("✅ 校验器自检通过" if not failed else f"共 {len(failed)} 项自检失败")
    return 1 if failed else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description="Custom 冷检查")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录")
    parser.add_argument("--self-test", action="store_true", help="只跑校验器自检")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    audit = Audit(args.root.resolve())
    audit.run()
    return audit.report()


if __name__ == "__main__":
    sys.exit(main())
