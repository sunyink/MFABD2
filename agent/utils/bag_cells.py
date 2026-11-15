"""背包列表整页读图：把一张截图切成格子，每格一次同全部图标比对。不依赖框架，只用 numpy 与 Pillow。

只回答「这一页每个格子是什么」：空格、认得出的图标（对应哪些物品）、有东西但认不出。点哪些格子、跳过哪些、
什么时候能判 0，由 bag_scan_engine 决定。图标图集与物品表由 tools/gen_bag_templates.py 生成（agent/data）。
设计见 priv doc/agent/bag/背包仓检_设计.md §16。

比对用带遮罩的归一化相关（与 OpenCV TM_CCOEFF_NORMED 加 mask、框架 TemplateMatch 加 green_mask 同一口径，
分数可与 §14 的标定直接对照）：图标透明处不参与，各通道先去均值。所有图标摊平成一个矩阵，
一页的格子摊平成另一个，一次矩阵乘法得到「每个图标 × 每个格子」的分数。
"""

from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
from PIL import Image


DATA = Path(__file__).resolve().parents[1] / "data"
_GREEN = np.array([0, 255, 0], np.uint8)
_FLAT_VARIANCE = 9.0     # 有效区里每像素每通道的方差低于它（约 3 个灰阶），当作纯色


@lru_cache(maxsize=1)
def item_table() -> dict:
    """tools/gen_bag_templates.py 生成的物品表：{packs: {包: 图集排布}, items: [{cn, tw, alias, profile, kind, icon, color?}]}。"""
    return json.loads((DATA / "bag_item_names.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Icon:
    """图集里的一块图标。names：用这张图的物品（目录物品在前，目录外的带 # 前缀）；多于一个时只能靠点开读名区分。"""
    names: tuple
    kinds: frozenset
    profiles: frozenset
    color: dict | None = None      # {lower, upper, count}：近亲的颜色核，count 已按本包有效像素换算


class IconPack:
    """一个资源包的图标图集，摊平成比对用的矩阵。像素按截图的通道顺序（BGR）存。"""

    def __init__(self, name: str):
        table = item_table()
        meta = table["packs"][name]
        self.name = name
        self.cell = tuple(meta["cell"])
        self.band = tuple(meta["band"])
        width, height = self.cell[0], self.band[1] - self.band[0]
        self.shape = (height, width)
        with Image.open(DATA / meta["atlas"]) as image:
            atlas = np.asarray(image.convert("RGB"))
        by_stem = {}
        for item in table["items"]:
            if item.get("icon"):
                by_stem.setdefault(item["icon"], []).append(item)
        tiles, masks, icons, seen = [], [], [], {}
        for index, stem in enumerate(meta["icons"]):
            r, c = divmod(index, meta["columns"])
            tile = atlas[r * height:(r + 1) * height, c * width:(c + 1) * width]
            # 目录物品排前面：同一张图既有目录物品又有目录外物品时，名字以目录的为先。
            owners = sorted(by_stem.get(stem, []), key=lambda item: item["profile"] == "other")
            key = tile.tobytes()
            if key in seen:      # 不同物品用了像素完全相同的图：并成一块，靠读名区分
                icons[seen[key]] = _merge(icons[seen[key]], owners)
                continue
            mask = ~(tile == _GREEN).all(axis=2)
            seen[key] = len(icons)
            tiles.append(tile[:, :, ::-1].astype(np.float32))
            masks.append(mask)
            icons.append(_merge(None, owners, int(mask.sum())))
        self.icons = icons
        mask = np.stack(masks).astype(np.float32)                      # K × h × w
        self.count = mask.sum(axis=(1, 2))                             # 每个图标的有效像素数
        pixels = np.stack(tiles) * mask[..., None]
        mean = pixels.sum(axis=(1, 2)) / self.count[:, None]           # K × 3
        centered = (np.stack(tiles) - mean[:, None, None, :]) * mask[..., None]
        self._tiles = centered.reshape(len(icons), -1)                 # K × (h·w·3)
        self._norm = np.sqrt((self._tiles ** 2).sum(axis=1))
        self._masks = mask.reshape(len(icons), -1)                     # K × (h·w)
        self.boxes = [_bbox(m) for m in masks]                         # 每个图标有效区的外接框（比对区内坐标）

    def scores(self, patches: np.ndarray) -> np.ndarray:
        """patches：C × h × w × 3（BGR，float32）。返回 K × C 的分数。"""
        cells = patches.shape[0]
        flat = patches.reshape(cells, -1)
        numerator = self._tiles @ flat.T
        planes = patches.reshape(cells, -1, 3)
        variance = np.zeros_like(numerator)
        for channel in range(3):
            plane = planes[:, :, channel]
            total = self._masks @ plane.T
            variance += self._masks @ (plane * plane).T - total * total / self.count[:, None]
        # 几乎纯色的一块（空格子）方差接近 0，除出来的分数没有意义：一律记 0。
        flat_enough = variance < self.count[:, None] * 3 * _FLAT_VARIANCE
        denominator = self._norm[:, None] * np.sqrt(np.maximum(variance, 1e-6))
        return np.where(flat_enough, 0.0, numerator / np.maximum(denominator, 1e-6))


def _merge(icon, owners, mask_pixels=0):
    # 目录外物品的名字前加 #：它可能与目录物品同名（简体「牛奶」），不能混成一个。
    labels = tuple(item["cn"] if item["profile"] != "other" else "#" + item["cn"] for item in owners)
    names = tuple(dict.fromkeys((icon.names if icon else ()) + labels))
    kinds = (icon.kinds if icon else frozenset()) | {item["kind"] for item in owners}
    profiles = (icon.profiles if icon else frozenset()) | {item["profile"] for item in owners}
    color = icon.color if icon else None
    for item in owners:
        spec = item.get("color")
        if spec and color is None:
            color = {"lower": spec["lower"], "upper": spec["upper"],
                     "count": max(1, round(spec["ratio"] * mask_pixels))}
    return Icon(names, frozenset(kinds), frozenset(profiles), color)


def _bbox(mask):
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@lru_cache(maxsize=2)
def icon_pack(name: str) -> IconPack:
    return IconPack(name)


def hsv(bgr: np.ndarray) -> np.ndarray:
    """BGR（uint8）→ HSV，OpenCV 8 位口径：H 0–180、S 与 V 0–255。只用于小块的颜色核。"""
    pixels = bgr.astype(np.float32)
    b, g, r = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    high = pixels.max(axis=-1)
    span = high - pixels.min(axis=-1)
    safe = np.where(span == 0, 1, span)
    hue = np.where(high == r, (g - b) / safe, np.where(high == g, 2 + (b - r) / safe, 4 + (r - g) / safe)) * 60
    hue = np.where(span == 0, 0, np.where(hue < 0, hue + 360, hue)) / 2
    saturation = np.where(high == 0, 0, span / np.where(high == 0, 1, high) * 255)
    return np.stack([np.rint(hue), np.rint(saturation), high], axis=-1)


def color_count(bgr: np.ndarray, lower, upper) -> int:
    values = hsv(bgr)
    inside = np.ones(values.shape[:2], bool)
    for channel in range(3):
        inside &= (values[..., channel] >= lower[channel]) & (values[..., channel] <= upper[channel])
    return int(inside.sum())


@dataclass(frozen=True)
class Grid:
    """列表在截图里的排布（1280×720 坐标），由资源包的节点给出。"""
    area: tuple            # 列表区 [x, y, w, h]
    columns: int
    left: float            # 第一列格子的左沿
    pitch: tuple           # (列距, 行距)
    footer: int = 0        # 列表区最下方压着固定底栏的行数
    row_offset: int = 0    # 图标相对「格子边框估出的行上沿」的竖直偏移；整页对齐失败时用它
    reach: int = 7         # 整页对齐时在 row_offset 上下各试几个像素
    shift: int = 1         # 对齐后逐格再细调几个像素（列距取整、个别格子的偏差）
    known: float = 0.85    # 认定一个图标的最低分
    margin: float = 0.03   # 认定时要领先另一个图标的分差
    empty_std: float = 8.0   # 比对区灰度标准差低于它、且没有数量字，算空格
    text_value: int = 200    # 数量字的亮度下限


@dataclass
class Cell:
    row: int                       # 本页第几行（只数完整露出的行，自上而下）
    col: int
    box: list                      # 格子 [x, y, w, h]
    empty: bool = False
    icon: Icon | None = None       # 认定的图标；None = 有物品但认不出
    score: float = 0.0             # 最高分图标的分数（认不出时也给，供排查）
    runner: float = 0.0            # 另一个图标的最高分
    guess: tuple = ()              # 最高分图标的物品名（认不出时也给）
    patch: np.ndarray | None = field(default=None, repr=False)   # 比对区原像素，供同一趟里认「同一张图」

    @property
    def target(self) -> list:
        """点击用的框：格子中间一块，避开边框与角标。"""
        x, y, w, h = self.box
        return [x + w // 4, y + h // 4, w // 2, h // 2]


@dataclass
class Page:
    tops: list                     # 完整露出的各行上沿 y
    cells: list
    aligned: bool = True           # 整页对齐有没有把握：较好的一半格子平均认得出才算有；没把握的页不该凭图标下结论

    def occupied(self):
        return [cell for cell in self.cells if not cell.empty]

    def ends_with_blanks(self) -> bool:
        """列表到底了：出现空格，且它之后（按阅读顺序）再没有物品。背包满格时没有空格，由调用方另判滑不动。"""
        seen_blank = False
        for cell in self.cells:
            if cell.empty:
                seen_blank = True
            elif seen_blank:
                return False
        return seen_blank


def _row_phase(gray: np.ndarray, grid: Grid, cell: tuple) -> float:
    """行上沿的大致相位：格子上下沿是固定行距的亮度跳变，把跳变强度按行距折叠后取最强的。只求大致，精确对齐靠比对。"""
    _, y, _, h = grid.area
    columns = []
    for col in range(grid.columns):
        left = int(round(grid.left + col * grid.pitch[0]))
        columns.append(gray[:, left + 4:left + cell[0] - 4].mean(axis=1))
    edge = np.abs(np.diff(np.mean(columns, axis=0)))
    pitch = grid.pitch[1]
    best = None
    for start in range(y - int(pitch), y + 1):
        score = 0.0
        for k in range(int(h / pitch) + 3):
            top = int(start + k * pitch)
            if top >= 1 and top + cell[1] - 1 < len(edge):
                score += edge[top - 1] + edge[top + cell[1] - 1]
        if best is None or score > best[0]:
            best = (score, start)
    return float(best[1])


def _crop(frame, x, y, shape):
    height, width = shape
    y = min(max(y, 0), frame.shape[0] - height)
    x = min(max(x, 0), frame.shape[1] - width)
    return frame[y:y + height, x:x + width]


def read_page(image: np.ndarray, grid: Grid, pack: IconPack) -> Page:
    """整页读图：每个完整露出的格子给出 空 / 认定的图标 / 认不出。image 为 BGR 截图。"""
    frame = np.asarray(image)
    gray = frame.astype(np.float32).mean(axis=2)
    cell_w, cell_h = pack.cell
    band_top = pack.band[0]
    _, area_y, _, area_h = grid.area
    bottom = area_y + area_h - grid.footer
    pitch_x, pitch_y = grid.pitch
    phase = _row_phase(gray, grid, pack.cell)
    lefts = [int(round(grid.left + col * pitch_x)) for col in range(grid.columns)]
    rough = [int(round(phase + k * pitch_y)) for k in range(-1, int(area_h / pitch_y) + 3)]

    def align(reach):
        """每行挑内容最丰富的三格，在 row_offset 上下 reach 个像素里试，取「认得最好的那一半格子的平均分」最大的偏移。

        只看较好的一半：一页里常有认不出的物品（箱子、新出的活动道具），不能让它们把对齐拖垮。返回 (分数, 偏移)。
        """
        inner = [top for top in rough if top - reach + grid.row_offset >= area_y - 1
                 and top + reach + grid.row_offset + cell_h <= bottom + 1]
        samples = []
        for top in inner:
            row = sorted(((float(_crop(gray, left, top + band_top + grid.row_offset, pack.shape).std()), left)
                          for left in lefts), reverse=True)[:3]
            samples += [(left, top) for std, left in row if std >= grid.empty_std]
        if not samples:
            return None
        tries = list(range(grid.row_offset - reach, grid.row_offset + reach + 1))
        patches = np.stack([_crop(frame, left, top + band_top + dy, pack.shape)
                            for dy in tries for left, top in samples]).astype(np.float32)
        peak = np.sort(pack.scores(patches).max(axis=0).reshape(len(tries), len(samples)), axis=1)
        peak = peak[:, len(samples) // 2:].mean(axis=1)
        return float(peak.max()), tries[int(peak.argmax())]

    # 整页对齐。先在估出的行上沿附近找；分数够不上「认得出」就把整个行距找一遍——
    # 行上沿是按亮度跳变估的，格子里的数量字也按行距重复，有时会抢到最强的位置，估出来的行差出十几像素
    # （2026-11-14 实机：那一页只有一格凑巧对上别的图标，被当成已读过的物品跳过，段尾给真物品判了 0）。
    # 对齐有没有把握以「认得出」为准：没把握的页由引擎逐格点开读名，不凭图标下结论。
    found = align(grid.reach)
    if found is None or found[0] < grid.known:
        wide = align(int(pitch_y // 2))
        if wide is not None and (found is None or wide[0] > found[0]):
            found = wide
    offset, aligned = (found[1], found[0] >= grid.known) if found else (grid.row_offset, False)

    tops = [top + offset for top in rough if top + offset >= area_y - 1 and top + offset + cell_h <= bottom + 1]
    cells = [Cell(row, col, [left, top, cell_w, cell_h])
             for row, top in enumerate(tops) for col, left in enumerate(lefts)]
    if not cells:
        return Page(tops, cells, aligned)

    shifts = [(dy, dx) for dy in range(-grid.shift, grid.shift + 1) for dx in range(-grid.shift, grid.shift + 1)]
    best = np.full((len(pack.icons), len(cells)), -1.0, np.float32)
    best_shift = np.zeros((len(pack.icons), len(cells)), np.int16)
    for index, (dy, dx) in enumerate(shifts):
        patches = np.stack([_crop(frame, cell.box[0] + dx, cell.box[1] + band_top + dy, pack.shape)
                            for cell in cells]).astype(np.float32)
        scores = pack.scores(patches)
        better = scores > best
        best[better] = scores[better]
        best_shift[better] = index

    for number, cell in enumerate(cells):
        x, y = cell.box[0], cell.box[1] + band_top
        patch = _crop(frame, x, y, pack.shape)
        text = frame[cell.box[1] + int(cell_h * 0.74):cell.box[1] + cell_h - 2, x + 4:x + cell_w - 2]
        has_text = int((text.min(axis=2) >= grid.text_value).sum()) >= 6
        if float(patch.astype(np.float32).mean(axis=2).std()) < grid.empty_std and not has_text:
            cell.empty = True
            continue
        column = best[:, number]
        chosen, runner = None, 0.0
        for candidate in np.argsort(column)[::-1][:6]:
            icon = pack.icons[candidate]
            score = float(column[candidate])
            if icon.color and score >= grid.known - 0.1:
                dy, dx = shifts[int(best_shift[candidate, number])]
                x0, y0, x1, y1 = pack.boxes[candidate]
                region = frame[y + dy + y0:y + dy + y1, x + dx + x0:x + dx + x1]
                if color_count(region, icon.color["lower"], icon.color["upper"]) < icon.color["count"]:
                    continue        # 形状像但颜色不对：近亲，不算候选
            if chosen is None:
                chosen = (candidate, score)
            else:
                runner = score
                break
        cell.score, cell.runner = (chosen[1] if chosen else 0.0), runner
        cell.guess = pack.icons[chosen[0]].names if chosen else ()
        if chosen and chosen[1] >= grid.known and chosen[1] - runner >= grid.margin:
            cell.icon = pack.icons[chosen[0]]
        else:
            cell.patch = patch.copy()
    return Page(tops, cells, aligned)


def same_picture(a: np.ndarray, b: np.ndarray, limit: float = 6.0) -> bool:
    """两个格子的比对区是不是同一张图（同一物品占了几格）：逐像素差的平均值很小。"""
    return a.shape == b.shape and float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) < limit
