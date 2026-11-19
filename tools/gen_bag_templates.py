"""由游戏原图批量生成背包仓检的图标图集与物品表（人工维护脚本，CI 不调用）。

产出（每次全量重写，先在内存里生成、数量过关才落盘）：
    agent/data/bag_icons_base.png、bag_icons_pc.png   图标图集：每个图标一块「整格对齐」的小图，透明处涂纯绿
    agent/data/bag_item_names.json                    物品表：规范名、繁体名、档案、类别、图标、近亲颜色范围；各包的图集排布

背包仓检在 py 里把整页截图切成格子，每格一次同全部图标比对（agent/utils/bag_cells.py），所以图标按格子原样
对齐存放、不裁外接框；识别节点不再逐个物品找图。目录外的物品（活动道具、亲密饮品、宝箱）也收进图集，档案记 other：
认得出它们，才能不点开就跳过。

用法（素材路径按本机实际给）：
    python tools/gen_bag_templates.py --text "F:/Git BD2/Asset-N-Text/2026-10-29" \
        --icons "F:/Git BD2/Asset-N" --icons "F:/Git BD2/Brown-Dust-2-Asset/ui/icon"
    改完跑 node tools/format-json.cjs agent/data/bag_item_names.json，再跑 tools/verify_bag_scan.py。
    加 --dump <目录> 另存每个图标的单张小图，供人工查看。

素材：
    --text   Asset-N-Text 快照目录：item_names.jsonl（物品名、表、编号、icon 字段）与 databases/common.db（分类列；
             宝箱的名字与图标也从这里读，见 load_boxes）
    --icons  原图目录，可给多个，按顺序优先；递归找 icon 字段同名的 PNG，只用 256×256 的那份
             （Asset-N 同名还有带 _#编号 的图集切片，尺寸不一，不能用）。Asset-N 根目录文件极多，扫一遍要几分钟。

变换与验证见 priv doc/agent/bag/背包仓检_设计.md §14.3；目录范围见 §14.5；逐格比对见 §16。
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
DATA = ROOT / "agent" / "data"
GREEN = (0, 255, 0)
ATLAS_COLUMNS = 24

# 两个平台的格子与缩放（设计 §14.3）：256 画布缩到 n、居中贴进格子；上沿 skip_top 行有稀有度菱形与料理星级。
PLATFORMS = {
    "base": {"n": 110, "cell": (94, 94), "skip_top": 16, "filtering": "mip2"},
    "pc": {"n": 92, "cell": (79, 78), "skip_top": 13, "filtering": "trilinear"},
}
# 两个平台的游戏画面采样不同（推测是贴图质量设置，未核实）。11-12 两平台全部已标注格子（宽搜索窗）对比，
# 自格最低分：模拟器 mip2 0.913 / trilinear 0.881 / mip1 0.858；PC trilinear 0.821 / mip1 0.803 / mip2 0.765。
KEEP_TOP = 0.70          # 下沿压着数量字，只留上部 70%
ALPHA_SOLID = 250        # 不透明度低于它的像素（含半透明描边）不参与匹配
MIN_MASK_PIXELS = 200    # 有效像素太少的图标不可靠（例：烟花棒只剩十几个像素），不收
MIN_ICONS = 300          # 找到的图标少于它多半是素材路径给错了，不落盘

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
# 不在消耗品页的类别（服装图样），不收。
SKIP_RESOURCE_TYPES = {2}

# 近亲的颜色核（设计 §14.4）：(lower, upper, 比例)，HSV 用 OpenCV 口径（H 0–180）；
# 比例 = 图标有效区里落在范围内的像素 / 有效像素。比例取「自己格子最低」与「冒认格子最高」之间（11-12 两平台标定）。
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
    table = json.loads((DATA / "bd2_name_norm_tw2cn.json").read_text(encoding="utf-8"))
    return lambda name: table.get(name, name)


def load_items(text_dir):
    """按物品表生成物品清单：[{profile, kind, cn, tw, alias, icon}]。

    profile：ingredients / dishes / growth / craft 是目录里要盘点的；other 是消耗品页里目录外的物品
    （活动道具、亲密饮品、目录外的券），只为认出来好跳过。目录物品同名只留第一条；other 与目录物品同名的照收
    （简体「牛奶」既是食材也是一个活动道具），由扫描核心按图标区分。
    """
    canon = canonical_names()
    db = sqlite3.connect(str(Path(text_dir) / "databases" / "common.db"))
    resource_type = dict(db.execute("select id, type from ResourceTable"))
    food_type = dict(db.execute("select id, foodType from FoodTable"))
    items, seen, other_seen = [], set(), set()
    for line in (Path(text_dir) / "item_names.jsonl").open(encoding="utf-8"):
        row = json.loads(line)
        raw = row["names"].get("zh-Hans") or ""
        cn = canon(raw)
        icons = [icon for icon in (row["icon"] if isinstance(row["icon"], list) else [row["icon"]]) if icon]
        if not cn or not icons:
            continue
        found = None
        if row["table"] == "FoodTable":
            found = ("ingredients", "食材") if food_type.get(row["id"]) == 2 else ("dishes", "料理")
        elif row["table"] == "ResourceTable":
            kind = resource_type.get(row["id"])
            if kind in SKIP_RESOURCE_TYPES:
                continue
            if kind in RESOURCE_TYPES:
                found = RESOURCE_TYPES[kind]
            elif kind == 0:
                found = next((("growth", label) for label, pattern in TYPE0_KEEP if re.search(pattern, raw)), None)
            if found is None:
                label = "饮品" if kind == 7 else ("其他券" if raw.endswith("券") else "活动道具")
                if (cn, icons[0]) not in other_seen:
                    other_seen.add((cn, icons[0]))
                    items.append({"profile": "other", "kind": label, "cn": cn,
                                  "tw": row["names"].get("zh-Hant") or "", "alias": [], "icon": icons[0]})
                continue
        if found is None or cn in seen:
            continue
        seen.add(cn)
        items.append({"profile": found[0], "kind": found[1], "cn": cn, "tw": row["names"].get("zh-Hant") or "",
                      "alias": [raw] if raw != cn else [], "icon": icons[0]})
    # 宝箱表里也有「只装一样东西、直接借那样东西的图标和名字」的条目（如名为初级天赋技能书的奖励），收进来会给目标物品的
    # 图标挂上一个同名的目录外别名；还有开箱奖励（付费钻石、金币、米饭）与活动礼包。所以图标已被物品表用过的、名字与已有
    # 物品相同的、名字不带「箱」的都不收（用户 11-12：箱子多半以「箱」结尾）。没收的礼包万一在背包里，只是点开一次读名。
    used_icons = {item["icon"] for item in items}
    used_names = {item["cn"] for item in items}
    for cn, tw, icon in load_boxes(db):
        cn = canon(cn)
        if icon in used_icons or cn in used_names or "箱" not in cn or (cn, icon) in other_seen:
            continue
        other_seen.add((cn, icon))
        items.append({"profile": "other", "kind": "宝箱", "cn": cn, "tw": tw, "alias": [], "icon": icon})
    return items


def _proto(blob):
    """不带结构定义地读一条 protobuf：{字段号: 值}，同号取最后一个；变长整数与字符串够用，其余跳过。"""
    def varint(at):
        value = shift = 0
        while True:
            byte = blob[at]
            at += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return value, at
    out, at = {}, 0
    while at < len(blob):
        key, at = varint(at)
        field, wire = key >> 3, key & 7
        if wire == 0:
            out[field], at = varint(at)
        elif wire == 2:
            size, at = varint(at)
            chunk = bytes(blob[at:at + size])
            at += size
            try:
                out[field] = chunk.decode("utf-8")
            except UnicodeDecodeError:
                out[field] = chunk
        elif wire in (1, 5):
            at += 8 if wire == 1 else 4
        else:
            break
    return out


def load_boxes(db):
    """宝箱：不在 item_names.jsonl 里，名字与图标在 RandomBoxTable / RandomBoxTextTable（protobuf，按字段号读）。

    RandomBoxTable：3 图标、7 名字的文本编号（6 是说明）；RandomBoxTextTable：4 简体、7 繁体。
    11-12 实测这样对出来的名字与模拟器上点开读到的 11 种箱名一致。箱子的数量没有用，收进来只为认出来好跳过、不点开。
    返回 [(简体名, 繁体名, 图标)]，没有名字或图标的行不收。
    """
    texts = {key: _proto(blob) for key, blob in db.execute("select id, ProtoBuf from RandomBoxTextTable")}
    boxes = []
    for _, blob in db.execute("select id, ProtoBuf from RandomBoxTable"):
        row = _proto(blob)
        icon, text = row.get(3), texts.get(row.get(7), {})
        cn, tw = text.get(4), text.get(7)
        if isinstance(icon, str) and icon.startswith("icon_") and isinstance(cn, str) and cn.strip():
            tw = tw if isinstance(tw, str) else ""
            boxes.append((re.sub(r"\s+", "", cn), re.sub(r"\s+", "", tw), icon))     # 有的名字里带换行
    return boxes


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


def band(platform):
    """图标在格子里参与比对的行范围 [y0, y1)：去掉上沿的稀有度菱形与下沿的数量字。"""
    cfg = PLATFORMS[platform]
    return cfg["skip_top"], int(cfg["cell"][1] * KEEP_TOP)


def render(rgba, platform):
    """256×256 原图 → (整格宽 × 比对行高的 RGB 小图, 有效像素数)；无效像素涂纯绿。有效像素不足返回 (None, 数)。"""
    cfg = PLATFORMS[platform]
    n, (cw, ch) = cfg["n"], cfg["cell"]
    scaled = scale_icon(rgba, n, cfg.get("filtering", "mip1"))
    ox, oy = (n - cw) // 2, (n - ch) // 2
    tile = scaled[oy:oy + ch, ox:ox + cw]
    mask = _erode((tile[:, :, 3] >= ALPHA_SOLID).astype(np.uint8))
    y0, y1 = band(platform)
    mask[y1:, :] = 0
    mask[:y0, :] = 0
    count = int(mask.sum())
    if count < MIN_MASK_PIXELS:
        return None, count
    out = tile[y0:y1, :, :3].copy()
    out[mask[y0:y1] == 0] = GREEN
    return out, count


def write_json(path, data):
    """保持原文件的换行符；排版交给 tools/format-json.cjs。"""
    raw = path.read_bytes() if path.exists() else b""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = json.dumps(data, ensure_ascii=False, indent=4) + "\n"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="由游戏原图批量生成背包仓检的图标图集与物品表")
    parser.add_argument("--text", required=True, help="Asset-N-Text 快照目录")
    parser.add_argument("--icons", action="append", required=True, help="原图目录，可给多个，按顺序优先")
    parser.add_argument("--dump", help="另存每个图标单张小图的目录（<目录>/<包>/<图标>.png），供人工查看")
    args = parser.parse_args(argv)

    items = load_items(args.text)
    catalog_names = {item["cn"] for item in items if item["profile"] != "other"}
    stray = set(COLORS) - catalog_names
    if stray:
        print(f"颜色表里的名字不在目录里（多半是规范名写错）：{'、'.join(sorted(stray))}")
        return 2
    paths = find_icons(args.icons, [item["icon"] for item in items])
    stems = sorted({item["icon"] for item in items if item["icon"] in paths})
    if len(stems) < MIN_ICONS:
        print(f"只找到 {len(stems)} 个图标的原图，少于 {MIN_ICONS}：检查 --icons 路径。没有写任何文件。")
        return 2

    sources = {}
    for stem in stems:
        with Image.open(paths[stem]) as image:
            sources[stem] = np.asarray(image.convert("RGBA"))
    packs, atlases, sparse = {}, {}, []
    for pack, cfg in PLATFORMS.items():
        kept, tiles = [], []
        for stem in stems:
            tile, count = render(sources[stem], pack)
            if tile is None:
                sparse.append(f"{stem}({pack}:{count})")
                continue
            kept.append(stem)
            tiles.append(tile)
        y0, y1 = band(pack)
        height, width = y1 - y0, cfg["cell"][0]
        rows = -(-len(tiles) // ATLAS_COLUMNS)
        atlas = np.zeros((rows * height, ATLAS_COLUMNS * width, 3), np.uint8)
        atlas[:, :] = GREEN
        for index, tile in enumerate(tiles):
            r, c = divmod(index, ATLAS_COLUMNS)
            atlas[r * height:(r + 1) * height, c * width:(c + 1) * width] = tile
        atlases[pack] = atlas
        packs[pack] = {"atlas": f"bag_icons_{pack}.png", "cell": list(cfg["cell"]), "band": [y0, y1],
                       "columns": ATLAS_COLUMNS, "icons": kept}
        if args.dump:
            out_dir = Path(args.dump) / pack
            out_dir.mkdir(parents=True, exist_ok=True)
            for stem, tile in zip(kept, tiles):
                Image.fromarray(tile, "RGB").save(out_dir / f"{stem}.png")

    for pack, atlas in atlases.items():
        Image.fromarray(atlas, "RGB").save(DATA / packs[pack]["atlas"], optimize=True)
    rows = []
    for item in items:
        row = {key: item[key] for key in ("cn", "tw", "alias", "profile", "kind")}
        row["icon"] = item["icon"] if item["icon"] in paths else None
        if item["profile"] != "other" and item["cn"] in COLORS:
            lower, upper, ratio = COLORS[item["cn"]]
            row["color"] = {"lower": lower, "upper": upper, "ratio": ratio}
        rows.append(row)
    write_json(DATA / "bag_item_names.json", {
        "source": Path(args.text).name,
        "note": "tools/gen_bag_templates.py 生成，别手改。背包消耗品页的物品：档案 other 是目录外的，只为认出来好跳过。",
        "packs": packs,
        "items": rows,
    })

    by_profile = {}
    for item in items:
        by_profile[item["profile"]] = by_profile.get(item["profile"], 0) + 1
    missing = [item["cn"] for item in items if item["profile"] != "other" and item["icon"] not in paths]
    print(f"物品 {len(items)}（{by_profile}），图标 {len(stems)} 个；图集 "
          + "、".join(f"{pack} {len(meta['icons'])} 块" for pack, meta in packs.items()))
    print(f"目录物品没找到 256×256 原图 {len(missing)}：{'、'.join(missing)}（照样能盘点：点开读名）")
    print(f"有效像素不足 {len(sparse)}：{'、'.join(sparse)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
