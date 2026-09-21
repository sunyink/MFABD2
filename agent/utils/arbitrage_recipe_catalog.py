"""从运行时料理队列发现菜谱，并生成补做所需的局部禁用与清计数清单。

只读取协议字段，不执行节点或修改上下文。get_node_data返回的整节点不能
直接回灌；只构造局部enabled覆盖与清计数清单，菜单路线和出口由Pipeline维护。
"""

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .name_i18n import canon


COOKING_HUBS = ("Arbitrage_Cooking_MenuEnter", "Arbitrage_Cooking_Page1")
REPLENISH_END = "Arbitrage_Cooking_Replenish_End"
COOKING_EXIT_ANCHOR = "Cooking_Exit"
_SUBMENU = "Arbitrage_Cooking_SubMenu"
_UNLIMITED = 2 ** 32 - 1


@dataclass(frozen=True)
class RecipeEntry:
    entry: str
    selectors: tuple[str, ...]
    name: str | None
    enabled: bool
    clear_hit_nodes: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class ReplenishSelection:
    recipes: tuple[RecipeEntry, ...]
    pipeline_override: dict
    clear_hit_nodes: tuple[str, ...]
    skipped: dict[str, str]


class _Reader:
    def __init__(self, context):
        self.context = context
        self.cache = {}

    def node(self, name):
        if name not in self.cache:
            data = self.context.get_node_data(name)
            if not isinstance(data, dict):
                raise ValueError(f"料理引用节点不存在或不可读: {name}")
            self.cache[name] = data
        return self.cache[name]

    def links(self, name):
        # Maa的读取接口已将JumpBack/Anchor前缀归一为属性。
        links = self.node(name).get("next", [])
        if not isinstance(links, list) or any(
            not isinstance(link, dict) or not isinstance(link.get("name"), str) for link in links
        ):
            raise ValueError(f"料理候选列表格式错误: {name}")
        if any(link.get("anchor") and link["name"] != COOKING_EXIT_ANCHOR for link in links):
            raise ValueError(f"料理候选包含未解析的动态Anchor: {name}")
        # The business entry chooses the terminal exit after recipe discovery.
        # It is not a recipe/menu edge and must not depend on a previous anchor value.
        return [link["name"] for link in links if not link.get("anchor")]

    def limited(self, names):
        return tuple(name for name in dict.fromkeys(names)
                     if 0 < self.node(name).get("max_hit", _UNLIMITED) < _UNLIMITED)

    def enabled(self, names):
        return all(self.node(name).get("enabled", True)
                   and self.node(name).get("max_hit", _UNLIMITED) != 0 for name in names)

    def recipe_names(self, recognition, visiting=()):
        if not isinstance(recognition, dict):
            raise ValueError("菜谱识别结构不可解析")
        kind = recognition.get("type")
        params = recognition.get("param", {})
        if not isinstance(params, dict):
            raise ValueError("菜谱识别参数不可解析")
        if kind == "TemplateMatch":
            paths = params.get("template", [])
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise ValueError("菜谱模板列表不可解析")
            names = set()
            for value in paths:
                path = PurePosixPath(value.replace("\\", "/"))
                if path.parent.as_posix() == "Shop/RecipeList" and path.stem.startswith("料理_"):
                    name = canon(path.stem.removeprefix("料理_"))
                    if name:
                        names.add(name)
            return names
        if kind not in ("And", "Or"):
            return set()
        children = params.get("all_of" if kind == "And" else "any_of", [])
        if not isinstance(children, list):
            raise ValueError("菜谱组合识别不可解析")
        names = set()
        for child in children:
            if isinstance(child, str):
                if child in visiting:
                    raise ValueError(f"菜谱识别引用循环: {child}")
                names.update(self.recipe_names(self.node(child).get("recognition"), (*visiting, child)))
            elif isinstance(child, dict):
                names.update(self.recipe_names(child, visiting))
            else:
                raise ValueError("菜谱子识别不可解析")
        return names


def discover_recipe_entries(context, hubs=COOKING_HUBS):
    """按菜单真实候选顺序发现Entry；未知结构保留在结果里供禁用，不猜菜名。

    hubs是既有菜单分组，不是菜谱白名单。新菜谱按现有Entry→Click选择节点→
    SubMenu结构接入任一分组即可纳管；新菜单分组可通过hubs参数补充。
    """
    hubs = tuple(hubs)
    if not hubs or len(set(hubs)) != len(hubs):
        raise ValueError("料理菜单分组为空或重复")
    reader = _Reader(context)
    recipes = []
    seen_hubs = set()
    seen_entries = set()

    def visit(hub, ancestors):
        if hub in ancestors:
            raise ValueError(f"料理菜单分组引用循环: {hub}")
        if hub in seen_hubs:
            return
        seen_hubs.add(hub)
        lineage = (*ancestors, hub)
        for entry in reader.links(hub):
            if entry in hubs:
                visit(entry, lineage)
                continue
            if not entry.startswith("Arbitrage_Cooking_") or not entry.endswith("_Entry"):
                continue
            if entry in seen_entries:
                raise ValueError(f"菜谱入口重复挂接: {entry}")
            seen_entries.add(entry)
            # 已知入口必须可读；不存在时整体拒绝，不能覆盖造出一个新节点。
            reader.node(entry)
            selectors = []
            name = None
            reason = ""
            try:
                for candidate in reader.links(entry):
                    node = reader.node(candidate)
                    if node.get("action", {}).get("type") == "Click" and _SUBMENU in reader.links(candidate):
                        selectors.append(candidate)
                if len(selectors) != 1:
                    raise ValueError("未找到唯一的菜谱选择节点")
                names = reader.recipe_names(reader.node(selectors[0]).get("recognition"))
                if len(names) != 1:
                    raise ValueError("模板无法对应唯一菜谱名称")
                name = names.pop()
            except ValueError as exc:
                reason = str(exc)
            nodes = (*lineage, entry, *selectors)
            recipes.append(RecipeEntry(entry, tuple(selectors), name,
                                       reader.enabled(nodes) and not reason,
                                       reader.limited(nodes), reason))

    visit(hubs[0], ())
    if not recipes:
        raise ValueError("料理队列没有可管理的菜谱入口")
    name_counts = Counter(recipe.name for recipe in recipes if recipe.name)
    selector_counts = Counter(node for recipe in recipes for node in recipe.selectors)
    return tuple(
        replace(recipe, enabled=False, reason="菜谱名称或选择节点重复，不能唯一派发")
        if ((recipe.name and name_counts[recipe.name] > 1)
            or any(selector_counts[node] > 1 for node in recipe.selectors)) else recipe
        for recipe in recipes
    )


def build_replenish_selection(entries, target_names):
    """只产生非目标false；目标沿用用户启用状态，不恢复整节点、不执行清计数。"""
    if isinstance(target_names, (str, bytes)):
        raise ValueError("补做菜谱必须是名称列表，不能传单个字符串")
    targets = set()
    for name in target_names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("补做菜谱名称无效")
        targets.add(canon(name.strip()))
    entries = tuple(entries)
    selected = tuple(recipe for recipe in entries if recipe.name in targets and recipe.enabled and not recipe.reason)
    selected_entries = {recipe.entry for recipe in selected}
    override = {}
    skipped = {}
    for recipe in entries:
        if recipe.entry in selected_entries:
            continue
        override[recipe.entry] = {"enabled": False}
        for node in recipe.selectors:
            override[node] = {"enabled": False}
        if recipe.reason:
            skipped[recipe.entry] = recipe.reason
        elif recipe.name in targets:
            skipped[recipe.name] = "用户禁用或菜单/入口命中上限为0"
    known = {recipe.name for recipe in entries}
    for name in sorted(targets - known):
        skipped[name] = "未在运行时料理队列中发现"
    # Shared menus remain on the original route even if all recipes on that page
    # are disabled. Reset their counts too, so that route can reach its exit.
    resets = tuple(dict.fromkeys(node for recipe in entries for node in recipe.clear_hit_nodes
                                 if recipe.entry in selected_entries or node in COOKING_HUBS))
    return ReplenishSelection(selected, override, resets, skipped)
