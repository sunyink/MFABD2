"""由游戏原图批量生成背包仓检的模板图与目录（人工维护脚本，CI 不调用）。

产出（每次全量重写）：
    assets/resource/{base,pc}/image/Shop/BagGen/<物品名>.png   生成的模板图（透明处涂纯绿，配 green_mask）
    assets/resource/{base,pc}/pipeline/BagScan.json            四个目录节点的 attach.templates
    agent/data/bag_item_names.json                              目录物品的简繁名（扫描读名用）

目录里已有手工截图的食材（Shop/BagList/食材_*.png）保留手工图；其余一律用生成图。
每个资源包只引用自己包里的图：PC 包缺的图不能退回 base（尺寸不同，会把有的物品扫成 0）。

用法（素材路径按本机实际给）：
    python tools/gen_bag_templates.py --text "F:/Git BD2/Asset-N-Text/2026-10-29" \
        --icons "F:/Git BD2/Asset-N" --icons "F:/Git BD2/Brown-Dust-2-Asset/ui/icon"
    改完跑 node tools/format-json.cjs 格式化两个 BagScan.json 与名录，再跑 tools/verify_bag_scan.py。

素材：
    --text   Asset-N-Text 快照目录：item_names.jsonl（物品名、表、编号、icon 字段）与 databases/common.db（分类列）
    --icons  原图目录，可给多个，按顺序优先；递归找 icon 字段同名的 PNG，只用 256×256 的那份
             （Asset-N 同名还有带 _#编号 的图集切片，尺寸不一，不能用）。Asset-N 根目录文件极多，扫一遍要几分钟。

变换与验证见 priv doc/agent/bag/背包仓检_设计.md §14.3；目录范围见 §14.5；颜色核见 §14.4。
"""

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
GREEN = (0, 255, 0)
GEN_DIR = "Shop/BagGen"

# 两个平台的格子与缩放（设计 §14.3）：256 画布缩到 n、居中贴进格子；上沿 skip_top 行有稀有度菱形与料理星级。
PLATFORMS = {
    "base": {"n": 110, "cell": (94, 94), "skip_top": 16, "filtering": "mip2"},
    "pc": {"n": 92, "cell": (79, 78), "skip_top": 13, "filtering": "trilinear"},
}
# 两个平台的游戏画面采样不同（推测是贴图质量设置，未核实）。11-12 两平台全部已标注格子（宽搜索窗）对比，
# 自格最低分：模拟器 mip2 0.913 / trilinear 0.881 / mip1 0.858；PC trilinear 0.821 / mip1 0.803 / mip2 0.765。
KEEP_TOP = 0.70          # 下沿压着数量字，只留上部 70%
ALPHA_SOLID = 250        # 不透明度低于它的像素（含半透明描边）不参与匹配
MIN_MASK_PIXELS = 200    # 有效像素太少的模板不可靠（例：烟花棒只剩十几个像素），不收
DEFAULT_THRESHOLD = 0.85

# 目录范围（用户 11-11 定稿，设计 §11 第 11–12 条）。type 0 是杂项，按名称再挑。
TYPE0_KEEP = [
    ("技能书", r"天赋技能书$"),
    ("精炼", r"^精炼(粉末|石|水晶)$"),
    ("升星", r"升星之星$"),
    ("契约书", r"招募契约书$"),
    ("票券", r"抽抽乐券$|抽抽乐兑换券$"),
]
RESOURCE_TYPES = {1: ("growth", "史莱姆"), 3: ("craft", "制作材料"), 4: ("growth", "突破材料"),
                  5: ("growth", "远古水晶"), 6: ("growth", "服装券")}
PROFILE_CATALOG = {"ingredients": "Agt_BagScan_Ingredients_Catalog", "dishes": "Agt_BagScan_Dishes_Catalog",
                   "growth": "Agt_BagScan_Growth_Catalog", "craft": "Agt_BagScan_Craft_Catalog"}

# 按类别放宽的阈值：券类彼此长得像、PC 上自己格子只有 0.87 左右，0.85 会漏（漏了就是误判 0）。
# 放宽只会多命中别的券格，那些格子本来也都要点开读，同一格一页只点一次（11-12 实测，设计 §14.3）。
THRESHOLD_BY_KIND = {"票券": 0.8, "服装券": 0.8}
THRESHOLDS = {}

# 近亲的颜色核：模板找候选、颜色挑格子（设计 §14.4）。只给阈值 0.85 下会互相冒认的族，以及用户点名的圣石。
# (lower, upper, 比例)：HSV 用 OpenCV 口径（H 0–180）；比例 = 命中框里落在范围内的像素 / 模板有效像素，
# 按各包模板的有效像素换算成 ColorMatch 的 count。比例取「自己格子最低」与「冒认格子最高」之间（11-12 两平台标定）。
COLORS = {
    "铁矿石": ([0, 0, 50], [180, 45, 220], 0.40),          # 自格 ≥0.85，冒认 ≤0.09
    "铜矿石": ([3, 100, 100], [12, 255, 255], 0.25),       # 自格 ≥0.62，冒认 ≤0.003
    "金矿石": ([15, 140, 140], [26, 255, 255], 0.25),      # 自格 ≥0.57，冒认 ≤0.004
    "铁块": ([0, 0, 30], [180, 50, 140], 0.80),            # 自格 ≥1.29，冒认 ≤0.40
    "钢铁块": ([95, 30, 40], [130, 140, 200], 0.60),       # 自格 ≥1.23，冒认 ≤0.27
    "白金块": ([0, 0, 120], [180, 45, 255], 0.30),         # 自格 ≥0.59，冒认 ≤0.12
    "铜块": ([3, 100, 80], [11, 255, 255], 0.35),          # 自格 ≥0.82，冒认 ≤0.09
    "黄铜块": ([11, 90, 90], [17, 255, 255], 0.30),        # 自格 ≥0.74，冒认 ≤0.04
    "金块": ([16, 140, 150], [26, 255, 255], 0.08),        # 自格 ≥0.18（模拟器偏低），冒认 0
    "普通招募契约书": ([0, 0, 120], [40, 90, 255], 0.30),  # 自格 ≥0.81，冒认 ≤0.006（PC 无样本）
    "高级招募契约书": ([15, 120, 150], [30, 255, 255], 0.30),
    "顶级招募契约书": ([115, 40, 120], [150, 255, 255], 0.15),
    "火圣石": ([0, 120, 150], [18, 255, 255], 0.30),       # 圣石未见冒认，按用户要求加；比例取自格最低的一半左右
    "风圣石": ([40, 80, 80], [75, 255, 255], 0.30),
    "水圣石": ([95, 70, 150], [118, 255, 255], 0.18),
    "黑暗圣石": ([115, 60, 30], [140, 255, 160], 0.40),
    "光明圣石": ([0, 0, 170], [180, 40, 255], 0.35),
}


def canonical_names():
    """项目的规范名规则（agent/utils/name_i18n.canon 用的同一张表）：游戏表里的简体名可能与项目规范名不同，
    例如物品表写「光圣石」，项目一直叫「光明圣石」。目录与名表一律用项目规范名，原名作别名。"""
    table = json.loads((ROOT / "agent" / "data" / "bd2_name_norm_tw2cn.json").read_text(encoding="utf-8"))
    return lambda name: table.get(name, name)


def load_items(text_dir):
    """按物品表生成三个科目四个档案的物品清单：[{profile, kind, cn, tw, alias, icon}]，同名只留第一条。"""
    canon = canonical_names()
    db = sqlite3.connect(str(Path(text_dir) / "databases" / "common.db"))
    resource_type = dict(db.execute("select id, type from ResourceTable"))
    food_type = dict(db.execute("select id, foodType from FoodTable"))
    items, seen = [], set()
    for line in (Path(text_dir) / "item_names.jsonl").open(encoding="utf-8"):
        row = json.loads(line)
        raw = row["names"].get("zh-Hans") or ""
        cn = canon(raw)
        icons = [icon for icon in (row["icon"] if isinstance(row["icon"], list) else [row["icon"]]) if icon]
        if not cn or not icons or cn in seen:
            continue
        found = None
        if row["table"] == "FoodTable":
            found = ("ingredients", "食材") if food_type.get(row["id"]) == 2 else ("dishes", "料理")
        elif row["table"] == "ResourceTable":
            kind = resource_type.get(row["id"])
            if kind in RESOURCE_TYPES:
                found = RESOURCE_TYPES[kind]
            elif kind == 0:
                found = next((("growth", label) for label, pattern in TYPE0_KEEP if re.search(pattern, raw)), None)
        if found is None:
            continue
        seen.add(cn)
        items.append({"profile": found[0], "kind": found[1], "cn": cn, "tw": row["names"].get("zh-Hant") or "",
                      "alias": [raw] if raw != cn else [], "icon": icons[0]})
    return items


def find_icons(dirs, stems):
    """在给定目录里递归找 stem（不带扩展名）同名或「stem_#编号」的 PNG，取 256×256 的那份。"""
    wanted, found = set(stems), {}
    for base in dirs:
        for current, _, files in os.walk(base):
            for name in files:
                if not name.endswith(".png"):
                    continue
                stem = name[:-4].split("_#")[0]
                if stem not in wanted or stem in found:
                    continue
                path = os.path.join(current, name)
                try:
                    with Image.open(path) as image:
                        if image.size == (256, 256):
                            found[stem] = path
                except OSError:
                    continue
    return found


def _bilinear(src, size):
    """与 OpenCV INTER_LINEAR 相同的采样位置（像素中心对齐、边缘钳位）；返回 float。"""
    h, w = src.shape[:2]
    scale_y, scale_x = h / size, w / size
    ys = np.clip((np.arange(size) + 0.5) * scale_y - 0.5, 0, h - 1)
    xs = np.clip((np.arange(size) + 0.5) * scale_x - 0.5, 0, w - 1)
    y0 = np.floor(ys).astype(int); x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, h - 1); x1 = np.minimum(x0 + 1, w - 1)
    fy = (ys - y0)[:, None, None]; fx = (xs - x0)[None, :, None]
    a = src[y0][:, x0]; b = src[y0][:, x1]; c = src[y1][:, x0]; d = src[y1][:, x1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


def _mip(src, level):
    """第 level 级 mip：每级 2×2 平均、边长减半。"""
    for _ in range(level):
        h, w = src.shape[:2]
        src = src.reshape(h // 2, 2, w // 2, 2, src.shape[2]).mean(axis=(1, 3))
    return src


def scale_icon(rgba, n, filtering="mip1"):
    """把 256×256 原图缩到 n×n，模拟游戏贴图的采样。

    mip1 / mip2：先逐级 2×2 平均缩到 128 / 64（第 1 / 2 级 mip），再双线性缩放到 n。
    trilinear：按缩小倍数在相邻两级 mip 之间线性混合（显卡的三线性过滤），level = log2(256/n)。
    选哪种按平台定，见 PLATFORMS。
    """
    src = rgba.astype(np.float64)
    if filtering == "trilinear":
        level = float(np.log2(256 / n))
        low = int(np.floor(level))
        weight = level - low
        out = (1 - weight) * _bilinear(_mip(src, low), n) + weight * _bilinear(_mip(src, low + 1), n)
    else:
        out = _bilinear(_mip(src, {"mip1": 1, "mip2": 2}[filtering]), n)
    return np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8)


def _erode(mask):
    """3×3 腐蚀，边界外视为有效（与 OpenCV erode 默认边界一致）。"""
    padded = np.pad(mask, 1, constant_values=1)
    out = np.ones_like(mask)
    for dy in range(3):
        for dx in range(3):
            out &= padded[dy:dy + mask.shape[0], dx:dx + mask.shape[1]]
    return out


def render(rgba, platform):
    """256×256 原图 → (模板 RGB 数组, 有效像素数)；有效像素不足返回 (None, 数)。"""
    cfg = PLATFORMS[platform]
    n, (cw, ch) = cfg["n"], cfg["cell"]
    scaled = scale_icon(rgba, n, cfg.get("filtering", "mip1"))
    ox, oy = (n - cw) // 2, (n - ch) // 2
    tile = scaled[oy:oy + ch, ox:ox + cw]
    mask = _erode((tile[:, :, 3] >= ALPHA_SOLID).astype(np.uint8))
    mask[int(ch * KEEP_TOP):, :] = 0
    mask[:cfg["skip_top"], :] = 0
    count = int(mask.sum())
    if count < MIN_MASK_PIXELS:
        return None, count
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    out = tile[y0:y1, x0:x1, :3].copy()
    out[mask[y0:y1, x0:x1] == 0] = GREEN
    return out, count


def file_name(cn):
    return re.sub(r'[\\/:*?"<>|]', "_", cn) + ".png"


def load_pipeline(pack):
    path = ROOT / "assets" / "resource" / pack / "pipeline" / "BagScan.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    """保持原文件的换行符；排版交给 tools/format-json.cjs。"""
    raw = path.read_bytes() if path.exists() else b""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = json.dumps(data, ensure_ascii=False, indent=4) + "\n"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="由游戏原图批量生成背包仓检的模板图与目录")
    parser.add_argument("--text", required=True, help="Asset-N-Text 快照目录")
    parser.add_argument("--icons", action="append", required=True, help="原图目录，可给多个，按顺序优先")
    args = parser.parse_args(argv)

    items = load_items(args.text)
    stray = (set(COLORS) | set(THRESHOLDS)) - {item["cn"] for item in items}
    if stray:
        print(f"颜色表或阈值表里的名字不在目录里（多半是规范名写错）：{'、'.join(sorted(stray))}")
        return 2
    icons = find_icons(args.icons, [item["icon"] for item in items])
    report = {"generated": 0, "no_icon": [], "too_sparse": []}
    templates = {pack: {} for pack in PLATFORMS}
    for pack in PLATFORMS:
        out_dir = ROOT / "assets" / "resource" / pack / "image" / GEN_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.png"):
            old.unlink()
        for item in items:
            path = icons.get(item["icon"])
            if path is None:
                report["no_icon"].append(item["cn"]) if pack == "base" else None
                continue
            with Image.open(path) as image:
                rgba = np.asarray(image.convert("RGBA"))
            tile, count = render(rgba, pack)
            if tile is None:
                report["too_sparse"].append(f"{item['cn']}({pack}:{count})")
                continue
            Image.fromarray(tile, "RGB").save(out_dir / file_name(item["cn"]), optimize=True)
            templates[pack][item["cn"]] = (f"{GEN_DIR}/{file_name(item['cn'])}", count)
            report["generated"] += 1

    for pack in PLATFORMS:
        path, pipeline = load_pipeline(pack)
        hand = dict(pipeline.get(PROFILE_CATALOG["ingredients"], {}).get("attach", {}).get("templates", {}))
        catalogs = {key: {} for key in PROFILE_CATALOG}
        for item in items:
            name = item["cn"]
            if item["profile"] == "ingredients" and isinstance(hand.get(name), str) and "BagList" in hand[name]:
                catalogs["ingredients"][name] = hand[name]       # 手工图保留，节点默认阈值
                continue
            if name not in templates[pack]:
                continue
            # 生成图的透明处是纯绿，必须开 green_mask；手工图条目保持原样（节点默认不开）。
            template, mask_pixels = templates[pack][name]
            threshold = THRESHOLDS.get(name, THRESHOLD_BY_KIND.get(item["kind"], DEFAULT_THRESHOLD))
            entry = {"template": template, "green_mask": True, "threshold": threshold}
            if name in COLORS:
                lower, upper, ratio = COLORS[name]
                entry["color"] = {"lower": lower, "upper": upper, "count": max(1, round(ratio * mask_pixels))}
            catalogs[item["profile"]][name] = entry
        for key, node in PROFILE_CATALOG.items():
            pipeline.setdefault(node, {}).setdefault("attach", {})["templates"] = catalogs[key]
        write_json(path, pipeline)

    names_path = ROOT / "agent" / "data" / "bag_item_names.json"
    write_json(names_path, {
        "source": Path(args.text).name,
        "note": "tools/gen_bag_templates.py 生成，别手改。背包仓检目录物品的规范简体名与繁体名（PC 客户端）。",
        "items": [{key: item[key] for key in ("cn", "tw", "alias", "profile", "kind")} for item in items],
    })
    by_profile = {}
    for item in items:
        by_profile[item["profile"]] = by_profile.get(item["profile"], 0) + 1
    print(f"物品 {len(items)}（{by_profile}），生成模板 {report['generated']} 张（两个包合计）")
    print(f"没找到 256×256 原图 {len(report['no_icon'])}：{'、'.join(report['no_icon'])}")
    print(f"有效像素不足 {len(report['too_sparse'])}：{'、'.join(report['too_sparse'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
