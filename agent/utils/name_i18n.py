"""
商品名简繁归一化（buy/sell 共用）

问题：pipeline/attach 里的商品清单统一写简体；但游戏客户端切繁体时 OCR 读到的是
繁体名（且含跨版本异义词，如 三文鱼↔鮭魚、黄油↔奶油、香草↔草藥），与简体清单精确
相等匹配就会全崩。

方案：把「任意端 OCR 名」与「attach 里任意语言写的词」都归一化到规范简体后再比。
pipeline 侧因此只需写简体一种语言，无需做繁体资源覆盖；用户自行改写 attach（简/繁
皆可）也能对齐（自己负责其内容正确）。

数据：agent/data/bd2_name_norm_tw2cn.json —— 纯「繁体名 → 简体名」映射，抓自
BD2DB(souseha) 站点全量道具（简繁双射、跨项零碰撞，详见项目记忆 shop-dict-data-source）。
简体名不是该表的 key，故 canon(简体) 走 .get 兜底原样返回 = 恒等；繁体名命中则转简体。

用法：
    from utils.name_i18n import canon
    canon("鮭魚")   -> "三文鱼"
    canon("三文鱼") -> "三文鱼"   # 恒等
    canon("某未收录名") -> 原样返回  # 兜底不误伤

【输入与输出】
    - 内部比较、计划和记账用 canon() 统一为简体物品名。
    - 回填界面 OCR 匹配词时用 name_variants() 展开简繁名称，无需判断客户端语言。
    - 原始 OCR 名仍可保存作观测证据；未收录名称保留原文，不猜测翻译。
"""

import json
from pathlib import Path
from . import mfaalog

# 纯繁→简映射；None=未加载，{}=加载失败/为空（canon 退化为恒等，不阻断主流程）
_NORM = None

# agent/utils/name_i18n.py → agent/ → agent/data/
_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "bd2_name_norm_tw2cn.json"


def _load() -> dict:
    """惰性加载并缓存繁→简表。文件缺失/损坏时回落空表（归一化=恒等），只告警不抛。"""
    global _NORM
    if _NORM is not None:
        return _NORM
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _NORM = data if isinstance(data, dict) else {}
        mfaalog.info(f"[NameI18n] 📖 简繁归一化表加载 {len(_NORM)} 条 ← {_DATA_FILE.name}")
    except Exception as e:
        _NORM = {}
        mfaalog.warning(
            f"[NameI18n] ⚠️ 简繁归一化表加载失败({e})，回落纯本地语言匹配"
            f"（繁体端商品名可能对不上简体清单）。缺失文件：{_DATA_FILE}"
        )
    return _NORM


def canon(name: str) -> str:
    """把任意端商品名归一化为规范简体；未收录名原样返回（兜底不误伤）。"""
    if not name:
        return name
    return _load().get(name, name)


def canon_set(names) -> set:
    """批量归一化为规范简体集合（供 attach 清单一次性转换）。"""
    return {canon(n) for n in names}


def name_variants(name: str) -> list[str]:
    """返回同一物品的规范简体、输入原文和已收录繁体名，去重并保留顺序。"""
    if not name:
        return []
    canonical = canon(name)
    return list(dict.fromkeys([canonical, name, *(
        traditional for traditional, simplified in _load().items() if simplified == canonical
    )]))
