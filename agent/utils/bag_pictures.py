"""背包仓检记住的目录外物品的图：认不出图标、点开读出是目录外的东西（活动道具等）的那一格，记下比对区的像素，
之后的盘点凭图跳过、不再点开（用户 2026-11-15：每次全盘要点开五六格活动道具，约 7 秒）。

- 只记目录外的（读到的名字不在名录里、或是名录里带 # 的目录外物品）。名录里的物品不记：它们可能是要读数的目标。
- 按图集分开存（`base` 模拟器、`pc` PC，格子像素不同），所有账号共用（图标与账号无关）：
  `<存档目录>/bag_pictures/<图集>.npz`，里面 `patches`（N × 高 × 宽 × 3，uint8）与 `meta`（JSON：名字、最后见到的时刻）。
- 超过 KEEP_DAYS 天没再见到的丢掉（活动结束、道具消失）；最多留 LIMIT 张，多了先丢最久没见到的。
- 读写失败只当没有记忆：扫描照常逐格点开，不影响读数。
"""

import json
import os
from pathlib import Path
import time

import numpy as np

KEEP_DAYS = 30
LIMIT = 300
FOLDER = "bag_pictures"


def _path(folder, pack) -> Path:
    return Path(folder) / FOLDER / f"{pack}.npz"


def load(folder, pack, *, now=None) -> list:
    """[{"patch", "name", "seen"}]；没有、读不了或过期的不返回。"""
    if folder is None:
        return []
    now = time.time() if now is None else now
    try:
        with np.load(_path(folder, pack), allow_pickle=False) as data:
            patches, meta = data["patches"], json.loads(str(data["meta"]))
    except (OSError, ValueError, KeyError):
        return []
    if len(meta) != len(patches):
        return []
    return [{"patch": patch, "name": str(row.get("name") or ""), "seen": float(row.get("seen") or 0)}
            for patch, row in zip(patches, meta) if now - float(row.get("seen") or 0) <= KEEP_DAYS * 86400]


def save(folder, pack, entries, *, now=None) -> bool:
    """整份写回（先写临时文件再替换）。entries 同 load 的返回；形状不一的只留占多数的那种。"""
    if folder is None:
        return False
    now = time.time() if now is None else now
    entries = [entry for entry in entries if now - entry["seen"] <= KEEP_DAYS * 86400]
    if entries:
        shapes = {}
        for entry in entries:
            shapes[entry["patch"].shape] = shapes.get(entry["patch"].shape, 0) + 1
        shape = max(shapes, key=lambda key: shapes[key])
        entries = sorted((entry for entry in entries if entry["patch"].shape == shape),
                         key=lambda entry: -entry["seen"])[:LIMIT]
    path = _path(folder, pack)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp.npz")
        patches = np.stack([entry["patch"] for entry in entries]).astype(np.uint8) if entries \
            else np.zeros((0, 1, 1, 3), np.uint8)
        meta = json.dumps([{"name": entry["name"], "seen": round(entry["seen"])} for entry in entries], ensure_ascii=False)
        np.savez_compressed(tmp, patches=patches, meta=np.array(meta))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def merge(known, seen_indexes, new, *, now=None) -> list:
    """这一趟见到过的旧图刷新时刻，新记的图加进去（与旧图或彼此同图的不重复记）。"""
    now = time.time() if now is None else now
    merged = [dict(entry, seen=now if index in seen_indexes else entry["seen"]) for index, entry in enumerate(known)]
    for patch, name in new:
        if not any(entry["patch"].shape == patch.shape and _close(entry["patch"], patch) for entry in merged):
            merged.append({"patch": patch, "name": name, "seen": now})
    return merged


def _close(a, b, limit=6.0) -> bool:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) < limit
