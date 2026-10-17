"""采集调试名单的只读配置访问，供目标选择与选卡流程共用。"""

import re

from .num_list import parse_number_list


DEBUG_LIST_NODE = "Collect_DebugList_User"
_LIST_KEY_BY_SECTION = {"Story": "StoryPack", "Character": "CharacterPack", "Event": "EventPack"}
_CARD_NAME_RE = re.compile(r"^(Story|Character|Event)_(\d+)")


def debug_list_numbers(context, node_name=DEBUG_LIST_NODE):
    """读调试节点的 attach；未启用、读取失败或空名单时返回 {}。"""
    try:
        data = context.get_node_data(node_name)
        if not isinstance(data, dict) or data.get("enabled") is False:
            return {}
        obj = context.get_node_object(node_name)
        attach = dict(getattr(obj, "attach", None) or {})
    except Exception:
        return {}
    out = {}
    for key, value in attach.items():
        numbers = parse_number_list(value)
        if numbers:
            out[key] = set(numbers)
    return out


def card_in_debug_list(card, debug_map):
    """card_name（如 Character_3_美丽无望）是否属于当前调试名单。"""
    if not debug_map or not card:
        return False
    match = _CARD_NAME_RE.match(str(card))
    if not match:
        return False
    key = _LIST_KEY_BY_SECTION.get(match.group(1))
    return int(match.group(2)) in debug_map.get(key, set())
