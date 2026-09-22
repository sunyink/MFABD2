"""静态核对套利采购目录、界面选项和 Pipeline 节点。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


FORCED_UNFAVORITES = {"天赋神药"}
ROOT = Path(__file__).resolve().parents[1]


def _name_set(values: Any) -> set[str]:
    return set(values) if isinstance(values, list) and all(isinstance(v, str) for v in values) else set()


def _split_items(value: Any) -> list[str] | None:
    if not isinstance(value, str):
        return None
    return [item.strip() for item in re.split(r"[，,;|]+", value) if item.strip()]


def validate(catalog: dict, interface: dict, pipeline: dict) -> list[str]:
    """返回所有配置差异；输入对象不会被修改。"""
    errors: list[str] = []
    if not isinstance(interface, dict) or not isinstance(interface.get("option"), dict):
        return ["interface.option 缺失或不是对象"]
    normalization = json.loads((ROOT / "agent/data/bd2_name_norm_tw2cn.json").read_text(encoding="utf-8"))
    cards = catalog.get("cartridges") if isinstance(catalog, dict) else None
    options = interface.get("option", {}) if isinstance(interface, dict) else {}
    nodes = pipeline if isinstance(pipeline, dict) else {}
    if not isinstance(cards, dict) or not cards:
        errors.append("catalog.cartridges 缺失、为空或不是对象")
        return errors
    default_node = nodes.get("Arbitrage_ShopBuy_Data_Csm")
    defaults = default_node.get("attach") if isinstance(default_node, dict) else None
    if not isinstance(defaults, dict):
        errors.append("Arbitrage_ShopBuy_Data_Csm.attach 缺失或不是对象")
    elif set(defaults) - set(cards) - {"ocr_exclude"}:
        errors.append(f"默认表多出卡带键: {sorted(set(defaults) - set(cards) - {'ocr_exclude'})}")
    task_options: set[str] = set()

    def collect(option_name: Any) -> None:
        if not isinstance(option_name, str) or option_name in task_options:
            return
        task_options.add(option_name)
        definition = options.get(option_name, {})
        for case in definition.get("cases", []) if isinstance(definition, dict) else []:
            if isinstance(case, dict):
                for child in case.get("option", []):
                    collect(child)
    for task in interface.get("task", []):
        if isinstance(task, dict) and task.get("entry") == "Arbitrage_Start":
            for option_name in task.get("option", []):
                collect(option_name)
    selector_root = nodes.get("Arbitrage_Buy_Select_Str", {})
    root_next = _name_set(selector_root.get("next"))

    for card, spec in cards.items():
        if not isinstance(spec, dict):
            errors.append(f"{card}: catalog 条目不是对象")
            continue
        code = card.split(":", 1)[0]
        raw_items = spec.get("items")
        if not isinstance(raw_items, list) or not all(isinstance(item, str) for item in raw_items):
            errors.append(f"{card}: catalog.items 必须是字符串列表")
            continue
        items = set(raw_items)
        allowed = items - FORCED_UNFAVORITES
        custom = spec.get("custom_node")
        selector = spec.get("selector")
        if not isinstance(custom, str):
            errors.append(f"{card}: custom_node 必须为节点名")
            continue
        option_name = f"采购商品-{code}"
        option = options.get(option_name)
        if not isinstance(option, dict):
            errors.append(f"{card}: 缺少界面选项 {option_name}")
            continue
        if option_name not in task_options:
            errors.append(f"{card}: {option_name} 未挂到 Arbitrage_Start")
        option_cases = option.get("cases")
        if not isinstance(option_cases, list):
            errors.append(f"{card}: {option_name}.cases 缺失或不是列表")
            option_cases = []
        seen: list[str] = []
        for case in option_cases:
            if not isinstance(case, dict) or not isinstance(case.get("name"), str):
                errors.append(f"{card}: 存在无商品名的 case")
                continue
            item = case["name"]
            seen.append(item)
            override = case.get("pipeline_override", {})
            if not isinstance(override, dict):
                errors.append(f"{card}: 商品 {item} override 不是对象")
                continue
            targets = set(override)
            if targets != {custom}:
                errors.append(f"{card}: 商品 {item} case 目标应为 {custom}，实际为 {sorted(targets)}")
            payload = override.get(custom, {})
            if not isinstance(payload, dict) or set(payload) != {"attach"}:
                errors.append(f"{card}: 商品 {item} override 应只覆盖 {custom}.attach")
                continue
            attach = payload["attach"]
            if not isinstance(attach, dict) or list(attach) != [item]:
                actual = list(attach) if isinstance(attach, dict) else attach
                errors.append(f"{card}: 商品 {item} attach 键错误，实际为 {actual}")
            elif attach[item] is not True:
                errors.append(f"{card}: 勾选商品 {item} 必须覆盖为 true")
        duplicates = sorted({item for item in seen if seen.count(item) > 1})
        if duplicates:
            errors.append(f"{card}: cases 商品重复 {duplicates}")
        expected_cases = allowed
        if set(seen) != expected_cases:
            errors.append(f"{card}: cases 商品集合差异，缺少 {sorted(expected_cases - set(seen))}，多出 {sorted(set(seen) - expected_cases)}")
        default = option.get("default_case", [])
        if not isinstance(default, list):
            errors.append(f"{card}: default_case 必须是列表")
        else:
            extra = set(default) - allowed if all(isinstance(item, str) for item in default) else [item for item in default if not isinstance(item, str)]
            if extra:
                errors.append(f"{card}: default_case 包含目录外商品 {sorted(extra, key=str)}")
        if isinstance(defaults, dict):
            if card not in defaults:
                errors.append(f"{card}: 默认表缺少卡带键")
            else:
                parsed = _split_items(defaults[card])
                if parsed is None:
                    errors.append(f"{card}: 默认表值必须是字符串")
                else:
                    selected = {normalization.get(item, item) for item in parsed} - FORCED_UNFAVORITES
                    extra = selected - {normalization.get(item, item) for item in allowed}
                    if extra:
                        errors.append(f"{card}: 默认表包含目录外商品 {sorted(extra)}")
        node = nodes.get(custom)
        if not isinstance(node, dict):
            errors.append(f"{card}: 不存在节点 {custom}")
        else:
            attach = node.get("attach")
            if not isinstance(attach, dict):
                errors.append(f"{card}: {custom}.attach 缺失或不是对象")
            else:
                keys = {item for item in attach if normalization.get(item, item) not in FORCED_UNFAVORITES}
                missing = allowed - keys
                extra = keys - allowed
                if missing or extra:
                    errors.append(f"{card}: {custom}.attach 键差异，缺少 {sorted(missing)}，多出 {sorted(extra)}")
                bad = [k for k, v in attach.items() if k in allowed and type(v) is not bool]
                if bad:
                    errors.append(f"{card}: {custom}.attach 非布尔键 {sorted(bad)}")
        if not isinstance(selector, str) or selector not in nodes:
            errors.append(f"{card}: 不存在 selector 节点 {selector}")
        elif selector not in root_next and f"[JumpBack]{selector}" not in root_next:
            errors.append(f"{card}: selector {selector} 未挂到 Arbitrage_Buy_Select_Str.next")
    return errors


def _load() -> tuple[dict, dict, dict]:
    read = lambda path, **kwargs: json.loads(Path(path).read_text(encoding="utf-8"), **kwargs)
    return (read(ROOT / "agent/data/arbitrage_shop_catalog.json"),
            read(ROOT / "assets/interface.json", strict=False),
            read(ROOT / "assets/resource/base/pipeline/Arbitrage.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    catalog, interface, pipeline = _load()
    errors = validate(catalog, interface, pipeline)
    if errors:
        print("套利采购配置校验失败：")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"套利采购配置校验通过：{len(catalog['cartridges'])} 个卡带")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
