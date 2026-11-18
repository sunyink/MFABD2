"""通用背包仓检回归：整页读图、按页扫描、判 0 前提、分段结清、两帧复核、恢复与停止；扫描记录存档。

全程合成画面与内存存档，不连接游戏、不读写用户真实存档。直接运行：
    python -B tools/verify_bag_scan.py -v
"""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from utils import arbitrage_store as store
from utils import inventory_store
from utils import bag_pictures
from utils.bag_cells import Cell, Grid, Icon, Page, color_count, hsv, icon_pack, item_table, match_picture, read_page
from utils.bag_corner import CornerLayout, corner_box, parse_corner
from utils.bag_detail import DetailLayout, FadeWatch, anchor_of, plain_quantity, split_quantity, stitch, text_box
from utils.bag_scan_engine import BagScanEngine, Reading, Section, estimate_shift, joining_kinds
from utils.ocr_item_name import item_aliases


GRID = [578, 62, 600, 634]
ROW = 100
END = "初级天赋技能书"
INGREDIENTS = {"兽肉", "香草", "盐", "黄油", "胡椒", "土豆", "米", "白糖", "鸡蛋", "小麦", "牛奶", "番茄", "蘑菇",
               "洋葱", "大蒜", "辣椒", "苹果", "葡萄", "蜂蜜", "杏仁", "芥末", "料酒", "肉桂", "樱桃"}
CRAFT = {"铁矿石", "铜矿石", "金矿石", "原木"}
TICKETS = {"券甲", "券乙", "券丙"}
QUANTITY_BOX = [270, 263, 90, 24]


def fillers(count, start=0):
    """填充格：别的食材（不在请求里），只用来把食材那一段拉长。"""
    return [f"杂物{start + index:03d}" for index in range(count)]


def others(count, start=0):
    """食材段之后的别类物品（读图认得出、不是目标）。"""
    return [f"别类{start + index:03d}" for index in range(count)]


FOOD = frozenset(INGREDIENTS | set(fillers(400)))
KNOWN = frozenset(INGREDIENTS | CRAFT | TICKETS | {"冰河魔石", "忍耐精髓"})
ALIASES = {**item_aliases(), **{name: name for name in KNOWN}}


def layout(*items, end=True):
    items = list(items) + ([END, "初级突破石"] if end else [])
    return [items[i:i + 6] + [None] * (6 - len(items[i:i + 6])) for i in range(0, len(items), 6)]


class Bag:
    """合成的背包列表：每行 6 格、行距 100，每个物品一块固定的随机纹理，滚动就是改视窗偏移。"""

    def __init__(self, rows, swipe=440, footer=0, toast_frames=0, blank_rows=0):
        # blank_rows：物品后面跟着的整行空格（背包没满时的样子）。
        self.rows = [list(row) for row in rows] + [[None] * 6 for _ in range(blank_rows)]
        self.swipe = swipe
        self.offset = 0
        # footer：压在列表区最下方、不随滚动的界面（实机模拟器约 64 行），列表留出同样的底部余量。
        self.footer = footer
        # toast_frames：前几次截图里底栏上半截叠着会消失的提示（10-28 第三轮试跑：底栏只有 17 行两帧相同）。
        self.toast_frames = toast_frames
        self.footer_pixels = np.random.default_rng(99).integers(0, 255, (footer, GRID[2], 3), np.uint8)
        height = len(self.rows) * ROW
        self.max_offset = max(0, height + footer - GRID[3])
        self.canvas = np.full((max(height + footer, GRID[3]), GRID[2], 3), 40, np.uint8)
        self.textures = {}
        for r, row in enumerate(self.rows):
            for c, item in enumerate(row):
                if item is None:
                    continue
                self.canvas[r * ROW + 10:r * ROW + 90, c * 100 + 10:c * 100 + 90] = self.texture(item)

    def texture(self, item):
        if item not in self.textures:
            seed = sum(map(ord, item)) * 7919 + len(self.textures)
            self.textures[item] = np.random.default_rng(seed).integers(0, 255, (80, 80, 3), np.uint8)
        return self.textures[item]

    def screen(self):
        image = np.full((720, 1280, 3), 20, np.uint8)
        x, y, w, h = GRID
        image[y:y + h, x:x + w] = self.canvas[self.offset:self.offset + h]
        if self.footer:
            image[y + h - self.footer:y + h, x:x + w] = self.footer_pixels
            if self.toast_frames:
                self.toast_frames -= 1
                image[y + h - self.footer:y + h - 17, x:x + w] = 230
        return image

    def visible_rows(self):
        """视窗里完整可见（没被底栏挡住）的行：(屏幕上的行上沿, 这一行的物品)。"""
        for r, row in enumerate(self.rows):
            top = r * ROW + 10 - self.offset
            if 0 <= top and top + 80 <= GRID[3] - self.footer:
                yield GRID[1] + top, row

    def item_at(self, box):
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        for top, row in self.visible_rows():
            for c, item in enumerate(row):
                left = GRID[0] + c * 100 + 10
                if item is not None and left <= cx < left + 80 and top <= cy < top + 80:
                    return item
        return None


class Device:
    def __init__(self, bag, quantities, *, sees=None, says=None, kinds=None, frames=None, list_faults=0,
                 stop_after_opens=None, open_failures=None, close_failures=0, false_bottoms=0, swipe_failures=0,
                 blank_names=(), unscalable=(), first_names=None, slow_items=(), settled=False, drifts=None,
                 drift_after_close=None, tw=False, late_items=()):
        self.bag = bag
        # late_items：点开慢半拍——点下去时列表还在、核对过一次之后弹窗才出来（11-15 模拟器实跑）。
        self.late_items = set(late_items)
        self.late = None
        self.tw = tw                        # 繁体客户端：数量写成「持有N個」（简体是「拥有N个」）
        # drifts：点这个物品时点击没成立、列表被带着挪了这么多像素（正数 = 列表内容上移）。11-15 PC 实机。
        self.drifts = dict(drifts or {})
        # drift_after_close：关掉这个物品的详情后，列表悄悄挪了这么多像素（下一次点击就点到别的格子上）。
        self.drift_after_close = dict(drift_after_close or {})
        self.settled = settled              # 读数是否取自设备核实过「已静止」的那一帧（MaaBagDevice 的截帧判定）
        self.detail_reads = 0               # 弹窗开着时又读了几次
        self.slow_items = set(slow_items)   # 点开了、但规定时间内没读清的物品（弹窗其实已经开着）
        self.clicks = []                    # 每次点格子点到的物品
        self.first_names = dict(first_names or {})   # 物品 → 刚点开那一帧读到的名字（之后恢复正常）
        self.unscalable = set(unscalable)   # 数量放大重读也读不出来的物品
        self.quantities = quantities
        # sees：读图把这个物品认成什么——别的名字、元组（几个物品共用的图标）、None（认不出）；不写就是认对了。
        self.sees = sees or {}
        self.says = says or {}              # 点开后详情上写的名字；不写就是物品本名
        self.kinds = kinds or {}            # 物品 → 类别（读图给图标带的类别）
        self.blank_names = set(blank_names)  # 点开后名字读不出来的物品
        self.frames = frames or {}          # 物品 → 依次出现的数量帧 [(文字, 框)]
        self.list_faults = list_faults
        self.stop_after_opens = stop_after_opens
        self.open_failures = dict(open_failures or {})
        self.close_failures = close_failures
        # false_bottoms：翻页没滑却报到底（SmartAction 截图失败、代理没跑起来都会这样）。
        self.false_bottoms = false_bottoms
        # swipe_failures：核对到底的补滑没有执行。
        self.swipe_failures = swipe_failures
        self.confirm_swipes = 0
        self.opens = []
        self.current = None
        self.frame_iter = None
        self.last = None
        self.persisted = []
        self.swipes = 0
        self.resets = 0
        self.corners = {}                   # 物品 → 格子右下角显示的数；没写就是读不出
        self.corner_reads = []              # 读过角标的格子是哪个物品

    def corner(self, image, cell):
        """仿 MaaBagDevice.read_corner：按格子位置找出物品，给它角标上的数。"""
        item = self.bag.item_at(cell.box)
        self.corner_reads.append(item)
        return self.corners.get(item)

    def stopping(self):
        return self.stop_after_opens is not None and len(self.opens) >= self.stop_after_opens

    def capture(self):
        return self.bag.screen()

    def list_ready(self, image):
        if self.list_faults:
            self.list_faults -= 1
            return False
        if self.late is not None:
            item, self.late = self.late, None
            if self.current is None:          # 这一次核对还看得到列表，之后弹窗才出来
                self.pending_popup = item
                return True
        if getattr(self, "pending_popup", None) is not None:
            self.current, self.pending_popup = self.pending_popup, None
        return self.current is None

    def read(self, image):
        """仿整页读图：按合成背包的当前视窗给出每格的认定结果。"""
        tops, cells = [], []
        for index, (top, row) in enumerate(self.bag.visible_rows()):
            tops.append(top)
            for col, item in enumerate(row):
                box = [GRID[0] + col * 100 + 10, top, 80, 80]
                if item is None:
                    cells.append(Cell(index, col, box, empty=True))
                    continue
                guess = self.sees.get(item, item)
                if guess is None:
                    cells.append(Cell(index, col, box, score=.4, patch=self.bag.texture(item)))
                    continue
                names = guess if isinstance(guess, tuple) else (guess,)
                icon = Icon(names, frozenset({self.kinds.get(item, "食材")}), frozenset())
                cells.append(Cell(index, col, box, icon=icon, score=.97, guess=names))
        return Page(tops, cells)

    def _name(self, item):
        if item in self.first_names:
            return [(self.first_names.pop(item), .99)]
        return [("", .2)] if item in self.blank_names else [(self.says.get(item, item), .99)]

    def _frame(self, item):
        if self.frame_iter is not None:
            self.last = next(self.frame_iter, self.last)
            text, box = self.last
            return Reading(names=self._name(item), quantities=[(text, .99, list(box))], settled=self.settled)
        return Reading(names=self._name(item), quantities=[(self._count(item), .99, list(QUANTITY_BOX))],
                       settled=self.settled)

    def _count(self, item):
        quantity = self.quantities.get(item, 0)
        return f"持有{quantity}個" if self.tw else f"拥有{quantity}个"

    def _drift(self, pixels):
        self.bag.offset = min(self.bag.max_offset, max(0, self.bag.offset + pixels))

    def open_detail(self, box):
        item = self.bag.item_at(box)
        self.clicks.append(item)
        if item in self.drifts:
            self._drift(self.drifts.pop(item))
            return None
        if item in self.late_items:
            self.late_items.discard(item)
            self.late = item
            return None
        if item in self.slow_items:
            self.slow_items.discard(item)
            self.opens.append(item)
            self.current = item
            return None
        if item is None or self.open_failures.get(item, 0):
            if item is not None:
                self.open_failures[item] -= 1
            return None
        self.opens.append(item)
        self.current = item
        sequence = self.frames.get(item)
        self.frame_iter = iter(sequence) if sequence else None
        self.last = sequence[-1] if sequence else None
        return self._frame(item)

    def peek_detail(self, box):
        """只等名称可读的点开：读数里没有数量。"""
        reading = self.open_detail(box)
        return Reading(names=reading.names, settled=self.settled) if reading is not None else None

    def read_detail(self):
        self.detail_reads += 1
        return self._frame(self.current) if self.current else None

    def read_names(self):
        return self._name(self.current) if self.current else []

    def read_quantity_scaled(self):
        if self.current in self.unscalable:
            return []
        return [self._count(self.current)]

    def close_detail(self):
        if self.close_failures:
            self.close_failures -= 1
            return False
        if self.current in self.drift_after_close:
            self._drift(self.drift_after_close.pop(self.current))
        self.current = None
        return True

    def next_page(self):
        """仿 SmartAction：画面变了报 moved，连续滑不动报 bottom。"""
        self.swipes += 1
        if self.false_bottoms:
            self.false_bottoms -= 1
            return "bottom"
        before = self.bag.offset
        self.bag.offset = min(self.bag.max_offset, before + self.bag.swipe)
        return "moved" if self.bag.offset != before else "bottom"

    def swipe(self):
        """仿直接 run_task 代理节点：只报滑动动作执行了没有。"""
        self.confirm_swipes += 1
        if self.swipe_failures:
            self.swipe_failures -= 1
            return False
        self.bag.offset = min(self.bag.max_offset, self.bag.offset + self.bag.swipe)
        return True

    def reset_to_top(self):
        self.resets += 1
        self.current = None
        self.bag.offset = 0
        return True


def food_section():
    return Section("存货", FOOD, "head", "ingredient_boundary")


def run(device, targets, *, sections=None, reader=None, **options):
    options.setdefault("on_page", lambda page, reads: device.persisted.append((page, reads)))
    options.setdefault("aliases", ALIASES)
    engine = BagScanEngine(device, reader or device.read, targets, known_names=KNOWN, grid_roi=GRID, row_height=ROW, columns=6,
                           sections=[food_section()] if sections is None else sections, **options)
    return engine.run()


def amounts(*names):
    return {name: 100 + index for index, name in enumerate(names)}


def persisted(device):
    return sorted(name for _, reads in device.persisted for name in reads)


class EngineTests(unittest.TestCase):
    def test_reads_everything_on_first_page_and_stops_early(self):
        names = ["兽肉", "米", "白糖", "鸡蛋", "小麦", "牛奶"]
        device = Device(Bag(layout(*names, *fillers(60))), amounts(*names))
        result = run(device, names)
        self.assertEqual((result["termination"], result["pages"], result["complete"]), ("all_found", 1, True))
        self.assertEqual(result["quantities"], amounts(*names))
        self.assertEqual(device.opens, names)           # 只点开目标；别的食材凭图标跳过
        self.assertEqual(device.swipes, 0)

    def test_section_end_settles_the_missing_without_scanning_on(self):
        # 食材段走完（连着两格认得出的别类物品）就地给段内没见到的判 0，后面十几页不再翻。
        bag = Bag(layout(*fillers(30), "兽肉", "米", *fillers(30, 30), *others(120), end=False))
        device = Device(bag, amounts("兽肉", "米"))
        result = run(device, ["兽肉", "米", "盐"])
        self.assertEqual((result["termination"], result["complete"]), ("section_end", True))
        self.assertEqual((result["quantities"], result["absent"]), (amounts("兽肉", "米"), ["盐"]))
        self.assertEqual(result["absent_by"], {"ingredient_boundary": ["盐"]})
        self.assertEqual(device.opens, ["兽肉", "米"])   # 段尾凭图标认定，不用点开
        self.assertLess(result["pages"], 5)

    def test_icons_on_a_page_without_confident_alignment_are_not_trusted(self):
        # 2026-11-14 实机：整页错位，鸡蛋那格凑巧对上已读过的别的物品的图标而没点开，段尾给鸡蛋判了 0。
        names, held = ["兽肉", "鸡蛋", "盐"], amounts("兽肉", "鸡蛋")
        layout_of = lambda: Bag(layout("兽肉", "米", "白糖", "小麦", "鸡蛋", *others(60), end=False))
        trusting = Device(layout_of(), held, sees={"鸡蛋": "兽肉"})
        self.assertEqual(sorted(run(trusting, names)["absent"]), ["盐", "鸡蛋"])   # 读图说对齐有把握时，认错的图标无从发现
        device = Device(layout_of(), held, sees={"鸡蛋": "兽肉"})

        def reader(image):
            page = device.read(image)
            page.aligned = device.bag.offset != 0      # 只有第一页（还没滑动时）对齐没把握
            return page

        result = run(device, names, reader=reader)
        self.assertEqual((result["quantities"], result["absent"]), (held, ["盐"]))   # 没有的照样判 0：身份都是读名读出来的
        self.assertIn("鸡蛋", device.opens)
        self.assertGreaterEqual(result["cells"]["unaligned_pages"], 1)
        self.assertTrue(result["complete"])

    def test_same_item_filling_rows_joins_two_pages_by_identity(self):
        # 2026-11-15 PC 实机：214 万泥炭连占三行，相邻两页重叠的部分全是同款，画面上分不清位移。
        # 上一页末尾与下一页开头是同一种物品，中间夹着的只可能还是它：没有漏掉别的东西，照常判 0。
        bag = Bag(layout("兽肉", *fillers(11), *["别类900"] * 42, *others(30)))
        device = Device(bag, amounts("兽肉"))
        result = run(device, ["兽肉", "盐"], sections=[])
        self.assertEqual((result["quantities"], result["absent"], result["unknown"]), (amounts("兽肉"), ["盐"], {}))
        self.assertEqual(result["continuity"], "verified")
        self.assertGreaterEqual(result["cells"]["joined_by_item"], 1)

    def test_either_of_the_last_two_items_joins_the_next_page(self):
        # 上一页末尾两种、对上哪一种都行：最后一格认错或认不出时，还有倒数第二种可接。
        self.assertEqual(joining_kinds(["布料", "布料", "泥炭", "泥炭"]), ["泥炭", "布料"])
        self.assertEqual(joining_kinds(["布料", "泥炭", None]), ["泥炭", "布料"])          # 最后一格认不出：跳过它
        self.assertEqual(joining_kinds(["布料", "泥炭", "认错的"]), ["认错的", "泥炭"])      # 最后一格认错：泥炭仍在
        self.assertEqual(joining_kinds(["泥炭"] * 8), ["泥炭"])
        self.assertEqual(joining_kinds(None), [])

    def test_pages_that_do_not_share_an_item_stay_broken(self):
        # 同款铺满几行，但下一页开头已经是上一页没见过的东西（中间可能漏了）：照旧判接不上。
        bag = Bag(layout("兽肉", *fillers(11), *["别类900"] * 42, *others(30)))
        device = Device(bag, amounts("兽肉"))
        pages = []

        def reader(image):
            page = device.read(image)
            pages.append(page)
            if len(pages) >= 2:      # 第二页起，把开头几格认成上一页没出现过的物品
                for cell in page.cells[:6]:
                    if cell.icon is not None:
                        cell.icon = Icon(("没见过的",), cell.icon.kinds, frozenset())
            return page

        result = run(device, ["兽肉", "盐"], sections=[], reader=reader)
        self.assertEqual(result["continuity"], "broken")
        self.assertEqual(result["unknown"], {"盐": "continuity_broken"})

    def test_one_stray_icon_inside_the_food_does_not_end_the_section(self):
        # 读图把一格食材错认成别类物品：只有一格、后面又是食材，不算走出这一段。
        bag = Bag(layout(*fillers(10), "盐", *others(12), end=False), blank_rows=2)
        device = Device(bag, amounts("盐"), sees={"杂物004": "别类999"})
        result = run(device, ["盐", "胡椒"])
        self.assertEqual((result["quantities"], result["absent"]), (amounts("盐"), ["胡椒"]))

    def test_unrecognised_item_after_the_food_is_opened_to_prove_the_end(self):
        # 食材后面第一格认不出（箱子）：点开读到目录外的名字，一格就算走出食材段。
        bag = Bag(layout(*fillers(8), "兽肉", "神秘宝箱", *others(60), end=False))
        device = Device(bag, amounts("兽肉"), sees={"神秘宝箱": None})
        result = run(device, ["兽肉", "盐"])
        self.assertEqual((result["termination"], result["absent"]), ("section_end", ["盐"]))
        self.assertEqual(device.opens, ["兽肉", "神秘宝箱"])
        self.assertEqual(result["pages"], 1)

    def test_blank_cells_mean_the_bottom_and_stop_without_extra_swipes(self):
        names = ["兽肉", "香草"]
        device = Device(Bag(layout(*fillers(40), *names, end=False), blank_rows=3), amounts(*names))
        result = run(device, [*names, "盐"], sections=[])
        self.assertEqual((result["termination"], result["zero_evidence"]), ("list_bottom", "list_bottom"))
        self.assertEqual((result["quantities"], result["absent"], result["complete"]), (amounts(*names), ["盐"], True))
        self.assertEqual(device.confirm_swipes, 0)       # 见到整行空格就停，不再空滑去试
        self.assertEqual(device.swipes, result["pages"] - 1)

    def test_full_bag_without_blanks_still_concludes_by_swiping_to_the_bottom(self):
        names = ["兽肉", "香草"]
        device = Device(Bag(layout(*fillers(40), *names, end=False)), amounts(*names))
        result = run(device, [*names, "盐"], sections=[])
        self.assertEqual((result["termination"], result["absent"], result["complete"]), ("list_bottom", ["盐"], True))
        self.assertEqual(device.confirm_swipes, 1)

    def test_blank_followed_by_an_item_is_not_the_bottom(self):
        # 列表是连续的：「空格」后面还认得出物品，说明空格判错了——照常读，但不许判 0。
        rows = layout(*fillers(6), end=False) + [["兽肉", None, "米", None, None, None]] + [[None] * 6]
        device = Device(Bag(rows), amounts("兽肉", "米"))
        result = run(device, ["兽肉", "米", "盐"], sections=[])
        self.assertEqual(result["quantities"], amounts("兽肉", "米"))
        self.assertEqual((result["absent"], result["unknown"]), ([], {"盐": "cell_unverified"}))

    def test_false_bottom_with_unexecuted_confirm_swipe_never_zeroes(self):
        names = ["兽肉", "香草"]
        device = Device(Bag(layout(*names, *fillers(60), end=False)), amounts(*names),
                        false_bottoms=1, swipe_failures=1)
        result = run(device, [*names, "盐"], sections=[])
        self.assertEqual((result["termination"], result["absent"]), ("error", []))
        self.assertEqual(result["unknown"], {"盐": "error"})
        self.assertEqual(result["quantities"], amounts(*names))

    def test_false_bottom_is_overruled_when_confirm_swipe_moves(self):
        device = Device(Bag(layout(*fillers(40), "兽肉", end=False)), amounts("兽肉"), false_bottoms=1)
        result = run(device, ["兽肉", "盐"], sections=[])
        self.assertEqual((result["termination"], result["quantities"], result["absent"]),
                         ("list_bottom", amounts("兽肉"), ["盐"]))

    def test_page_write_failure_turns_unsaved_reads_unknown(self):
        device = Device(Bag(layout("兽肉", *fillers(60))), amounts("兽肉"))

        def failing(page, reads):
            raise RuntimeError("存档只读")

        result = run(device, ["兽肉", "盐"], on_page=failing)
        self.assertEqual(result["quantities"], {})
        self.assertEqual(result["unknown"]["兽肉"], "persist_failed")
        self.assertFalse(result["complete"])

    def test_fixed_footer_run_reaches_real_bottom_and_concludes(self):
        # 10-28 首轮试跑的原样：底栏让每次翻页都「接不上」，最后一页又被底栏拉成「没动」而提前收尾。
        names = ["兽肉", "香草", "杏仁"]
        bag = Bag(layout(*fillers(20), names[0], *fillers(20, 20), names[1], *fillers(30, 40), names[2], end=False),
                  footer=64)
        device = Device(bag, amounts(*names))
        result = run(device, [*names, "盐"], sections=[], footer_rows=64)
        self.assertEqual((result["termination"], result["continuity"]), ("list_bottom", "verified"))
        self.assertEqual((result["quantities"], result["absent"]), (amounts(*names), ["盐"]))

    def test_changing_footer_needs_fixed_footer_rows(self):
        # 底栏上半截第一页叠着会消失的提示：只靠「两帧相同」自动剔除不够，要按资源包给出底栏行数。
        def scan(footer_rows):
            bag = Bag(layout(*fillers(40), "兽肉", end=False), footer=64, toast_frames=1)
            return run(Device(bag, amounts("兽肉")), ["兽肉", "盐"], sections=[], footer_rows=footer_rows)

        self.assertEqual(scan(0)["unknown"], {"盐": "continuity_broken"})
        self.assertEqual(scan(64)["absent"], ["盐"])

    def test_icon_and_name_that_keep_disagreeing_are_both_left_unverified(self):
        # 图文都相符才采信（用户 11-15）：读图把番茄认成苹果、点开读出番茄，先当列表挪了、重新分格；
        # 重新分格后还是对不上，就是图标和读名有一个错了——分不清是哪个，番茄不记数，苹果、番茄本轮都不判 0。
        device = Device(Bag(layout("番茄", *fillers(20)), blank_rows=2), amounts("番茄"), sees={"番茄": "苹果"})
        result = run(device, ["苹果", "番茄"])
        self.assertEqual((result["quantities"], result["absent"]), ({}, []))
        self.assertEqual(sorted(result["unknown"]), ["番茄", "苹果"])
        self.assertEqual(result["cells"]["resplit"], 1)

    def test_shared_icon_is_always_opened_and_the_missing_twin_is_zeroed(self):
        # 两个物品共用一张图：每一格都点开由读名定；有的那个读到，没有的那个到底判 0。
        twins = ("冰河魔石", "忍耐精髓")
        bag = Bag(layout(*["冰河魔石"] * 3, *others(10), end=False), blank_rows=2)
        device = Device(bag, {"冰河魔石": 30000}, sees={"冰河魔石": twins})
        result = run(device, list(twins), sections=[])
        self.assertEqual((result["quantities"], result["absent"]), ({"冰河魔石": 30000}, ["忍耐精髓"]))
        self.assertEqual(device.opens, ["冰河魔石"] * 3)

    def test_look_alike_kinds_are_opened_even_when_the_icon_says_already_read(self):
        # 券彼此相像：读图把券乙认成已经读过的券丙。按类别一律点开，券乙才不会被当成券丙跳过而误判 0。
        kinds = dict.fromkeys(TICKETS, "票券")
        bag = Bag(layout("券丙", "券乙", *others(10), end=False), blank_rows=2)
        device = Device(bag, amounts("券丙", "券乙"), sees={"券乙": "券丙"}, kinds=kinds)
        loose = dict(sections=[], loose_kinds={"票券"}, loose_names=TICKETS)
        result = run(device, ["券丙", "券乙", "券甲"], **loose)
        self.assertEqual((result["quantities"], result["absent"]), (amounts("券丙", "券乙"), ["券甲"]))

    def test_pattern_after_the_name_is_dropped(self):
        # 料理名后面跟着星级图案，会被读成一串相同的字符（实测「三角美乃滋饭团 ***」）：去掉后对得上名录就用它。
        device = Device(Bag(layout("番茄", "苹果", *fillers(20)), blank_rows=2), amounts("番茄", "苹果"),
                        says={"番茄": "番茄 ***", "苹果": "苹果米米米米米"})
        result = run(device, ["番茄", "苹果"])
        self.assertEqual(result["quantities"], amounts("番茄", "苹果"))

    def test_name_unclear_on_the_first_frame_is_read_again(self):
        # 数量先读全、名称还在出现动画里：第一帧名字不成样，弹窗还开着，再看一眼就对了。
        device = Device(Bag(layout("番茄", *fillers(20)), blank_rows=2), amounts("番茄"), first_names={"番茄": "番茄?*"})
        result = run(device, ["番茄"])
        self.assertEqual((result["quantities"], result["unknown"]), (amounts("番茄"), {}))

    def test_popup_that_opened_late_is_read_without_a_second_click(self):
        # 点开了但规定时间内没读清：弹窗已经开着，接着读就行；再点一次格子可能把弹窗点掉。
        for unknown_icon in (False, True):
            with self.subTest(unknown_icon=unknown_icon):
                device = Device(Bag(layout("番茄", *fillers(20)), blank_rows=2), amounts("番茄"), slow_items={"番茄"},
                                sees={"番茄": None} if unknown_icon else None)
                result = run(device, ["番茄"])
                self.assertEqual((result["quantities"], result["unknown"]), (amounts("番茄"), {}))
                self.assertEqual(device.clicks.count("番茄"), 1)

    def test_opened_cell_with_unreadable_name_blocks_zero(self):
        # 图标说是苹果、点开却读不出名字：这格是谁不知道，苹果本轮不许判 0。
        device = Device(Bag(layout("番茄", *fillers(20)), blank_rows=2), amounts("番茄"),
                        sees={"番茄": "苹果"}, blank_names={"番茄"})
        result = run(device, ["苹果", "盐"])
        self.assertEqual(result["unknown"], {"苹果": "detail_unreadable"})
        self.assertEqual(result["absent"], ["盐"])

    def test_name_read_without_quantity_stays_unverified(self):
        frames = {"番茄": [("拥有1", QUANTITY_BOX)]}
        device = Device(Bag(layout("番茄", *fillers(20)), blank_rows=2), {}, frames=frames, unscalable={"番茄"})
        result = run(device, ["番茄"])
        self.assertEqual((result["absent"], result["unknown"]), ([], {"番茄": "detail_unreadable"}))

    def test_unrecognised_cells_are_opened_once_and_the_same_picture_is_skipped(self):
        # 认不出的格子点开一次；同一张图再出现（同一物品占了几格）不重复点。目录外的名字读清楚了，不挡判 0。
        bag = Bag(layout(*["神秘宝箱"] * 4, *others(10), end=False), blank_rows=2)
        device = Device(bag, {}, sees={"神秘宝箱": None})
        result = run(device, ["铁矿石"], sections=[])
        self.assertEqual((device.opens, result["absent"], result["complete"]), (["神秘宝箱"], ["铁矿石"], True))
        self.assertEqual(result["cells"]["same_picture"], 3)

    def test_unrecognised_cell_that_is_a_target_gets_read(self):
        # 模板没认出来的目标物品：点开读名照常记数——漏认只多一次点开，不会变成 0。
        bag = Bag(layout(*["铁矿石"] * 2, *others(10), end=False), blank_rows=2)
        device = Device(bag, {"铁矿石": 12345}, sees={"铁矿石": None})
        result = run(device, ["铁矿石", "金矿石"], sections=[])
        self.assertEqual((result["quantities"], result["absent"]), ({"铁矿石": 12345}, ["金矿石"]))
        self.assertEqual(device.opens, ["铁矿石"])

    def test_unrecognised_cell_that_cannot_be_read_blocks_every_zero(self):
        bag = Bag(layout("神秘宝箱", *others(10), end=False), blank_rows=2)
        device = Device(bag, {}, sees={"神秘宝箱": None}, blank_names={"神秘宝箱"})
        result = run(device, ["铁矿石", "金矿石"], sections=[])
        self.assertEqual(result["absent"], [])
        self.assertEqual(result["unknown"], {"铁矿石": "cell_unverified", "金矿石": "cell_unverified"})

    def test_name_close_to_a_catalog_name_is_not_taken_as_foreign(self):
        # 读出来像目录物品的名字被截了一截：宁可当没读清，不能当成目录外物品放过。
        bag = Bag(layout("神秘宝箱", *others(10), end=False), blank_rows=2)
        device = Device(bag, {}, sees={"神秘宝箱": None}, says={"神秘宝箱": "鱼子酱罐"})
        result = run(device, ["铁矿石"], sections=[])
        self.assertEqual(result["unknown"], {"铁矿石": "cell_unverified"})

    def test_same_name_for_two_items_needs_the_icon_to_agree(self):
        # 简体「牛奶」既是食材也是一个活动道具：图标认不出时读到这个名字不能记数，也不许判 0。
        bag = Bag(layout("活动牛奶", *others(10), end=False), blank_rows=2)
        device = Device(bag, {"活动牛奶": 77}, sees={"活动牛奶": None}, says={"活动牛奶": "牛奶"})
        result = run(device, ["牛奶"], sections=[], ambiguous={"牛奶"})
        self.assertEqual((result["quantities"], result["unknown"]), ({}, {"牛奶": "detail_unreadable"}))
        # 图标认定就是食材牛奶时照常记数。
        device = Device(Bag(layout("牛奶", *others(10), end=False), blank_rows=2), {"牛奶": 5})
        result = run(device, ["牛奶"], sections=[], ambiguous={"牛奶"})
        self.assertEqual(result["quantities"], {"牛奶": 5})

    def test_milk_is_ambiguous_only_on_a_simplified_client(self):
        # 简体里活动道具也叫「牛奶」，繁体里它叫「純白牛奶」（11-15 PC 繁体实机）。认不出图标的格子读到「牛奶」：
        # 繁体客户端上它只会是食材，照常记数；简体客户端上分不清，不记、也不判 0。语言从前面读到的数量字样认。
        by_language = {"cn": {"牛奶"}, "tw": set()}
        for tw, expected in ((True, ({"兽肉": 3, "牛奶": 77}, {})), (False, ({"兽肉": 3}, {"牛奶": "detail_unreadable"}))):
            with self.subTest(tw=tw):
                bag = Bag(layout("兽肉", "牛奶", *others(10), end=False), blank_rows=2)
                device = Device(bag, {"兽肉": 3, "牛奶": 77}, sees={"牛奶": None}, tw=tw)
                result = run(device, ["兽肉", "牛奶"], sections=[], ambiguous=by_language)
                self.assertEqual((result["quantities"], result["unknown"]), expected)

    def test_event_milk_with_its_own_icon_is_the_foreign_twin(self):
        # 简体客户端：活动道具认出了自己的图标（#牛奶）、点开读到「牛奶」——图文相符，它是目录外那个，不算食材牛奶。
        bag = Bag(layout("活动牛奶", *others(10), end=False), blank_rows=2)
        device = Device(bag, {"活动牛奶": 9}, sees={"活动牛奶": "#牛奶"}, says={"活动牛奶": "牛奶"})
        result = run(device, ["牛奶"], sections=[], ambiguous={"cn": {"牛奶"}, "tw": set()})
        self.assertEqual((result["quantities"], result["absent"], result["cells"]["resplit"]), ({}, ["牛奶"], 0))

    def test_run_section_settles_after_leaving_it(self):
        # 制作材料在列表中间连成一段：进去再出来就结清，不必扫到底；进去之前见到别类物品不算数。
        craft = Section("装备制作材料", frozenset(CRAFT), "run")
        bag = Bag(layout(*others(20), "铁矿石", "铜矿石", *others(120, 100), end=False))
        device = Device(bag, amounts("铁矿石", "铜矿石"))
        result = run(device, ["铁矿石", "铜矿石", "金矿石"], sections=[craft])
        self.assertEqual((result["termination"], result["absent"]), ("section_end", ["金矿石"]))
        self.assertEqual(result["absent_by"], {"section_end": ["金矿石"]})
        self.assertLess(result["pages"], 4)

    def test_run_section_never_entered_waits_for_the_bottom(self):
        craft = Section("装备制作材料", frozenset(CRAFT), "run")
        device = Device(Bag(layout(*others(40), end=False), blank_rows=2), {})
        result = run(device, ["铁矿石"], sections=[craft])
        self.assertEqual((result["termination"], result["absent_by"]), ("list_bottom", {"list_bottom": ["铁矿石"]}))

    def test_food_settles_early_while_the_rest_goes_on_to_the_bottom(self):
        # 几种类型一起扫：食材段走完先结清食材，别的继续扫到列表底。
        bag = Bag(layout(*fillers(10), "兽肉", *others(30), "铁矿石", *others(6, 50), end=False), blank_rows=2)
        device = Device(bag, amounts("兽肉", "铁矿石"))
        result = run(device, ["兽肉", "盐", "铁矿石", "金矿石"])
        self.assertEqual(result["quantities"], amounts("兽肉", "铁矿石"))
        self.assertEqual(result["absent_by"], {"ingredient_boundary": ["盐"], "list_bottom": ["金矿石"]})

    def test_skipped_rows_never_produce_zero(self):
        names = ["兽肉"]
        device = Device(Bag(layout(*names, *fillers(60), end=False), swipe=560, blank_rows=2), amounts(*names))
        result = run(device, [*names, "盐"], sections=[])
        self.assertEqual((result["continuity"], result["absent"]), ("broken", []))
        self.assertEqual(result["unknown"], {"盐": "continuity_broken"})
        self.assertEqual(result["quantities"], amounts(*names))

    def test_quantity_is_taken_only_when_two_frames_agree(self):
        moving = [QUANTITY_BOX[0], QUANTITY_BOX[1] + 30, *QUANTITY_BOX[2:]]
        frames = {"兽肉": [("拥有1个", moving), ("拥有12个", QUANTITY_BOX), ("拥有12个", QUANTITY_BOX)]}
        device = Device(Bag(layout("兽肉", *fillers(20))), {}, frames=frames)
        self.assertEqual(run(device, ["兽肉"])["quantities"], {"兽肉": 12})

    def test_settled_frame_is_taken_without_reading_again(self):
        frames = {"兽肉": [(f"拥有{n}个", QUANTITY_BOX) for n in (12, 1, 123)]}
        device = Device(Bag(layout("兽肉", *fillers(20))), {}, frames=frames, settled=True)
        self.assertEqual(run(device, ["兽肉"])["quantities"], {"兽肉": 12})
        self.assertEqual(device.detail_reads, 0)

    def test_settled_peek_of_a_target_reads_the_quantity_once(self):
        device = Device(Bag(layout("兽肉", *fillers(20))), amounts("兽肉"), sees={"兽肉": None}, settled=True)
        self.assertEqual(run(device, ["兽肉"])["quantities"], amounts("兽肉"))
        self.assertEqual(device.detail_reads, 1)

    def test_never_stable_quantity_stays_unknown(self):
        frames = {"兽肉": [(f"拥有{n}个", QUANTITY_BOX) for n in (1, 12, 123, 1234, 12345)]}
        device = Device(Bag(layout("兽肉", *fillers(20)), blank_rows=2), {}, frames=frames)
        result = run(device, ["兽肉"])
        self.assertEqual((result["quantities"], result["unknown"]), ({}, {"兽肉": "detail_unreadable"}))

    def test_target_outside_the_item_table_is_unknown_never_zero(self):
        device = Device(Bag(layout("兽肉", *fillers(20))), amounts("兽肉"))
        result = run(device, ["兽肉", "灵魂鲜奶油"])
        self.assertEqual((result["unknown"], result["absent"]), ({"灵魂鲜奶油": "template_missing"}, []))

    def test_stop_keeps_confirmed_reads_and_leaves_the_rest_unknown(self):
        names = ["兽肉", "米", "白糖"]
        device = Device(Bag(layout(*names, *fillers(20))), amounts(*names), stop_after_opens=3)
        result = run(device, names)
        self.assertEqual((result["termination"], result["quantities"]), ("stopped", amounts("兽肉", "米")))
        self.assertEqual(result["unknown"], {"白糖": "stopped"})
        self.assertEqual(persisted(device), ["兽肉", "米"])

    def test_one_recovery_restarts_from_top_then_finishes(self):
        names = ["兽肉", "杏仁"]
        bag = Bag(layout(names[0], *fillers(40), names[1], end=False), blank_rows=2)
        device = Device(bag, amounts(*names), close_failures=2)
        result = run(device, [*names, "盐"], sections=[])
        self.assertEqual((result["recovered"], device.resets, result["termination"]), (True, 1, "list_bottom"))
        self.assertEqual((result["quantities"], result["absent"]), (amounts(*names), ["盐"]))
        self.assertEqual(device.opens.count("兽肉"), 1)      # 已读到的不重查

    def test_second_failure_ends_with_state_lost(self):
        device = Device(Bag(layout("兽肉", "米", *fillers(20))), amounts("兽肉", "米"), close_failures=9)
        result = run(device, ["兽肉", "米", "盐"])
        self.assertEqual((result["termination"], result["absent"]), ("state_lost", []))
        self.assertEqual(result["quantities"], amounts("兽肉", "米"))    # 读数先记下，再确认列表状态
        self.assertEqual(result["unknown"], {"盐": "state_lost"})

    def test_open_failure_marks_target_unverified(self):
        # 一直点不开：先重新分格再点（每页两次），用完了按原坐标补点，仍不开就记没核实。
        device = Device(Bag(layout("兽肉", *fillers(20)), blank_rows=2), amounts("兽肉"), open_failures={"兽肉": 9})
        result = run(device, ["兽肉"])
        self.assertEqual((result["absent"], result["unknown"]), ([], {"兽肉": "detail_unreadable"}))
        self.assertEqual(result["cells"]["resplit"], 2)

    def test_popup_that_opens_after_the_check_is_closed_before_splitting_again(self):
        # 点开慢半拍：核对时列表还在、判成没反应，重新分格去截图时弹窗才出来——关掉再截，不当成状态异常、不回顶部重扫。
        device = Device(Bag(layout("番茄", *fillers(20)), blank_rows=2), amounts("番茄"), late_items={"番茄"})
        result = run(device, ["番茄"])
        self.assertEqual((result["quantities"], result["unknown"], result["recovered"]), (amounts("番茄"), {}, False))
        self.assertEqual((result["cells"]["resplit"], device.resets), (1, 0))

    def test_small_strip_that_changed_is_not_taken_as_the_list_moving(self):
        # 点开没反应时只有底部一条变了（11-15 PC：按下的高亮 / 提示条，列表没动）：当列表没动接着做，照常判 0。
        bag = Bag(layout("兽肉", *fillers(20)), footer=64, toast_frames=2)
        device = Device(bag, amounts("兽肉"), open_failures={"兽肉": 1})
        result = run(device, ["兽肉", "盐"])
        self.assertEqual((result["quantities"], result["absent"], result["unknown"]), (amounts("兽肉"), ["盐"], {}))
        self.assertEqual(result["cells"]["resplit"], 1)

    def test_open_that_fails_once_is_retried_after_splitting_again(self):
        device = Device(Bag(layout("兽肉", *fillers(20)), blank_rows=2), amounts("兽肉"), open_failures={"兽肉": 1})
        result = run(device, ["兽肉"])
        self.assertEqual((result["quantities"], result["unknown"], result["cells"]["resplit"]), (amounts("兽肉"), {}, 1))



def engine_for(device, targets, **options):
    options.setdefault("on_page", lambda page, reads: device.persisted.append((page, reads)))
    options.setdefault("aliases", ALIASES)
    options.setdefault("sections", [])
    return BagScanEngine(device, device.read, targets, known_names=KNOWN, grid_roi=GRID, row_height=ROW, columns=6,
                         **options)


class PictureMemoryTests(unittest.TestCase):
    """记住目录外物品的图（用户 11-15）：第一次点开读出是目录外的，记下那一格的图；之后的盘点凭图跳过、不再点开。"""

    def bag(self):
        return Bag(layout(*["神秘宝箱"] * 2, "活动糖果", *others(10), end=False), blank_rows=2)

    def test_second_scan_skips_what_the_first_scan_learned(self):
        sees = {"神秘宝箱": None, "活动糖果": None}
        first = Device(self.bag(), {}, sees=sees)
        engine = engine_for(first, ["铁矿石"])
        result = engine.run()
        seen, new = engine.pictures()
        self.assertEqual((first.opens, result["absent"], seen), (["神秘宝箱", "活动糖果"], ["铁矿石"], set()))
        self.assertEqual([name for _, name in new], ["神秘宝箱", "活动糖果"])
        known = bag_pictures.merge([], set(), new, now=1000)
        second = Device(self.bag(), {}, sees=sees)
        engine = engine_for(second, ["铁矿石"], known_pictures=known)
        result = engine.run()
        self.assertEqual((second.opens, result["absent"], result["cells"]["known_picture"]), ([], ["铁矿石"], 3))
        self.assertEqual(engine.pictures(), ({0, 1}, []))

    def test_catalog_item_without_an_icon_is_never_remembered(self):
        # 模板没认出的目录物品可能是要读数的目标：照常点开，不记图。
        device = Device(Bag(layout("铁矿石", *others(10), end=False), blank_rows=2), {"铁矿石": 7}, sees={"铁矿石": None})
        engine = engine_for(device, ["铁矿石", "金矿石"])
        self.assertEqual(engine.run()["quantities"], {"铁矿石": 7})
        self.assertEqual(engine.pictures(), (set(), []))

    def test_named_outside_item_is_remembered_too(self):
        # 名录里带 # 的目录外物品（繁体「純白牛奶」）点开读出来也记。
        device = Device(Bag(layout("活动牛奶", *others(10), end=False), blank_rows=2), {}, sees={"活动牛奶": None},
                        says={"活动牛奶": "純白牛奶"})
        engine = engine_for(device, ["铁矿石"], aliases={**ALIASES, "純白牛奶": "#牛奶"})
        engine.run()
        self.assertEqual([name for _, name in engine.pictures()[1]], ["#牛奶"])

    def test_picture_matches_across_a_small_offset_only(self):
        rng = np.random.default_rng(5)
        picture = rng.integers(0, 255, (80, 80, 3), np.uint8)
        stack = np.stack([rng.integers(0, 255, (80, 80, 3), np.uint8), picture]).astype(np.float32)
        self.assertEqual(match_picture(stack, picture), 1)
        self.assertEqual(match_picture(stack, np.roll(picture, (1, -2), axis=(0, 1))), 1)   # 跨盘点格子错开一两个像素
        self.assertIsNone(match_picture(stack, np.roll(picture, 5, axis=0)))
        self.assertIsNone(match_picture(stack, rng.integers(0, 255, (80, 80, 3), np.uint8)))
        self.assertIsNone(match_picture(None, picture))

    def test_store_round_trip_expiry_and_dedup(self):
        rng = np.random.default_rng(7)
        a, b = (rng.integers(0, 255, (40, 40, 3), np.uint8) for _ in range(2))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(bag_pictures.load(folder, "pc"), [])
            day = 86400
            entries = bag_pictures.merge([], set(), [(a, "幽灵糖果"), (a.copy(), "幽灵糖果"), (b, "仙女棒")], now=100 * day)
            self.assertEqual([entry["name"] for entry in entries], ["幽灵糖果", "仙女棒"])     # 同图只记一张
            self.assertTrue(bag_pictures.save(folder, "pc", entries, now=100 * day))
            loaded = bag_pictures.load(folder, "pc", now=110 * day)
            self.assertEqual([entry["name"] for entry in loaded], ["幽灵糖果", "仙女棒"])
            self.assertTrue(np.array_equal(loaded[0]["patch"], a))
            self.assertEqual(bag_pictures.load(folder, "base", now=110 * day), [])          # 按图集分开
            refreshed = bag_pictures.merge(loaded, {1}, [], now=125 * day)                  # 仙女棒 125 天时又见到了
            bag_pictures.save(folder, "pc", refreshed, now=125 * day)
            self.assertEqual([entry["name"] for entry in bag_pictures.load(folder, "pc", now=140 * day)], ["仙女棒"])

DRIFT_FOOD = ["兽肉", "米", "白糖", "鸡蛋", "小麦", "番茄",
              "蘑菇", "洋葱", "大蒜", "辣椒", "苹果", "葡萄",
              "蜂蜜", "杏仁", "芥末", "料酒", "牛奶", "肉桂",
              "樱桃", "胡椒", "土豆", "香草", "黄油"]


class DriftTests(unittest.TestCase):
    """2026-11-15 PC 实机（ledger 试跑5）：点开没反应时列表被带着上移，后面按旧坐标点到下一行，
    读到的「牛奶」与格子图标对不上被当成目录外，存货段提前关掉、100 项判 0。"""

    def scan(self, **faults):
        targets = [*DRIFT_FOOD, "盐"]
        device = Device(Bag(layout(*DRIFT_FOOD, *fillers(30))), amounts(*DRIFT_FOOD), **faults)
        return device, run(device, targets, ambiguous={"牛奶"})

    def test_failed_open_that_drags_the_list_resplits_and_reads_on(self):
        for pixels in (ROW, 32):
            with self.subTest(pixels=pixels):
                device, result = self.scan(drifts={"大蒜": pixels})
                self.assertEqual((result["quantities"], result["absent"], result["unknown"]),
                                 (amounts(*DRIFT_FOOD), ["盐"], {}))
                self.assertEqual(result["cells"]["resplit"], 1)
                self.assertEqual(device.opens.count("大蒜"), 1)

    def test_name_that_does_not_match_the_icon_does_not_end_the_section(self):
        # 关掉辣椒后列表悄悄上移一行：下一格（苹果的旧坐标）点开是牛奶，牛奶又与亲密饮品同名。
        # 旧做法信图标、当它是目录外，存货段当场走完；现在重新分格接着做，全部读到。
        device, result = self.scan(drift_after_close={"辣椒": ROW})
        self.assertEqual((result["quantities"], result["absent"], result["unknown"]),
                         (amounts(*DRIFT_FOOD), ["盐"], {}))
        self.assertEqual(result["cells"]["resplit"], 1)

    def test_name_read_at_the_wrong_cell_is_not_taken(self):
        # 图文对不上的那一格不收读数（用户 11-15），重新分格后到它自己那格再点开读。
        device, result = self.scan(drift_after_close={"蘑菇": ROW})
        self.assertEqual(result["quantities"], amounts(*DRIFT_FOOD))
        self.assertEqual(device.opens.count("杏仁"), 2)    # 在洋葱的旧坐标上点开过一次，不收；自己那格再点一次

    def test_rows_that_scrolled_away_unread_block_every_zero(self):
        # 上移了三行：还没处理的那两行滚出屏幕，再也点不到——不知道它们是不是盐，本轮不判 0。
        device, result = self.scan(drifts={"米": 3 * ROW})
        self.assertEqual(result["absent"], [])
        self.assertEqual(result["unknown"].get("盐"), "cell_unverified")

    def test_shift_that_cannot_be_measured_blocks_every_zero(self):
        # 第二页往回挪（列表内容下移）：量不出挪了多少，整页重走，本轮不判 0。
        names = [*DRIFT_FOOD, *fillers(40)]
        device = Device(Bag(layout(*names, "盐")), amounts(*DRIFT_FOOD, "盐"), drifts={"盐": -150})
        result = run(device, [*DRIFT_FOOD, "盐", "鸡蛋"], ambiguous={"牛奶"})
        self.assertEqual((result["absent"], result["cells"]["resplit"]), ([], 1))
        self.assertEqual(result["quantities"], amounts(*DRIFT_FOOD, "盐"))


def detail_layout(pack="base"):
    data = json.loads((ROOT / "assets" / "resource" / pack / "pipeline" / "BagScan.json").read_text(encoding="utf-8"))
    return DetailLayout.from_attach(data["Agt_BagScan_Detail"]["attach"])


POPUP = (60, 47, 40)        # 弹窗底色（BGR）
BLUE = (253, 206, 161)      # 数量行的浅蓝字（BGR，实测 RGB 161,206,253）


def popup(name_value=255, name_width=120, quantity_rows=(264,), description_blue=None, slide=0):
    """合成一帧详情弹窗：名称白字一块、数量浅蓝字一行（rows 给行上沿）、右上角一个「×」；slide：整块还差几像素没滑到终点。"""
    image = np.zeros((720, 1280, 3), np.uint8)
    image[:] = POPUP
    image[198 + slide:222 + slide, 268:268 + name_width] = name_value
    for top in quantity_rows:
        image[top + slide:top + slide + 22, 270:360] = BLUE
    if description_blue is not None:
        image[description_blue + slide:description_blue + slide + 20, 300:400] = BLUE
    for k in range(16):                                       # 「×」：两条对角线
        image[180 + slide + k, 1000 + k] = 249
        image[180 + slide + k, 1015 - k] = 249
    return image


class DetailTests(unittest.TestCase):
    """详情弹窗的截帧判定与按颜色定字（agent/utils/bag_detail.py）。"""

    def test_both_packs_define_the_detail_bands(self):
        for pack in ("base", "pc"):
            with self.subTest(pack=pack):
                bands = detail_layout(pack)
                nx, ny, nw, nh = bands.name
                lx, ly, lw, lh = bands.name_lit
                self.assertTrue(nx == lx and ny == ly and lw < nw and lh == nh)    # 左段在名称行里
                self.assertGreater(bands.quantity[1], ny)
                self.assertGreater(bands.close_icon[0], nx + nw)                   # 「×」在名称行右边

    def test_without_an_anchor_the_fade_ends_after_three_still_frames(self):
        watch = FadeWatch(detail_layout())
        frames = (popup(slide=6, name_value=200), popup(slide=2), popup(), popup(), popup())
        self.assertEqual([watch.feed(f) for f in frames], [False, False, False, False, True])

    def test_with_an_anchor_the_close_icon_must_be_back_in_place(self):
        bands = detail_layout()
        watch = FadeWatch(bands, anchor_of(popup(), bands))
        # 越过终点 2 像素时文字也可能两帧一样（截图重复给同一帧），「×」不在原位就不算走完
        frames = (popup(slide=-2), popup(slide=-2), popup(), popup())
        self.assertEqual([watch.feed(f) for f in frames], [False, False, False, True])

    def test_text_boxes_follow_the_colour(self):
        bands = detail_layout()
        image = popup(name_width=200)
        self.assertEqual(text_box(image, bands.name, "white", bands), (262, 194, 212, 32))
        self.assertEqual(text_box(image, bands.quantity, "blue", bands, top_line=True), (264, 260, 102, 30))
        self.assertIsNone(text_box(popup(quantity_rows=()), bands.quantity, "blue", bands, top_line=True))

    def test_quantity_is_the_top_blue_line_even_when_moved_down(self):
        bands = detail_layout()
        box = popup(quantity_rows=(297,), description_blue=322)      # 宝箱：数量行下移，说明里还有蓝字
        self.assertEqual(text_box(box, bands.quantity, "blue", bands, top_line=True)[1], 293)

    def test_two_parts_are_stitched_into_one_line(self):
        canvas = stitch(popup(), [(262, 194, 132, 32), (264, 260, 102, 30)])
        self.assertEqual(canvas.shape, (32, 132 + 24 + 102, 3))
        self.assertEqual(tuple(int(v) for v in canvas[0, 140]), POPUP)       # 空隙填底色

    def test_stitched_text_is_split_at_the_quantity(self):
        cases = {"黄色史莱姆拥有2,876个": ("黄色史莱姆", "拥有2876个"), "红色史莱姆拥有9.391个": ("红色史莱姆", "拥有9391个"),
                 "1.5周年服装选择券 擁有3個": ("1.5周年服装选择券", "擁有3個"), "閃爍的粉末持有1個": ("閃爍的粉末", "持有1個"),
                 "拥有5个": None, "黄色史莱姆": None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(split_quantity(text), expected)

    def test_thousands_marks_are_dropped_only_when_the_grouping_is_regular(self):
        """穿透复核第 8 条：「1,23」多半是漏读了一位，不能去掉分隔符当 123 收下——原样交给扫描核心，它解析不了就放大重读。"""
        def parsed(quantity_text):                    # 扫描核心拿到这段数量文字后认出的数；认不出是 None
            return BagScanEngine._parse(Reading(quantities=[(quantity_text, 0.9, [0, 0, 1, 1])]))[0]

        regular = {"拥有2876个": 2876, "拥有2,876个": 2876, "擁有99.999個": 99999, "持有2,166,297個": 2166297,
                   "拥有1。234个": 1234, "拥有12 , 345 个": 12345, "拥有0个": 0}
        for text, value in regular.items():
            with self.subTest(text=text):
                self.assertEqual(parsed(plain_quantity(text)), value)             # 单读那条路
                self.assertEqual(parsed(split_quantity("白糖" + text)[1]), value)  # 拼读那条路
        for text in ("拥有1,23个", "拥有12,34个", "擁有1,234,56個", "持有1234,567個", "拥有1,,234个", "拥有,234个",
                     "拥有1,234.个", "拥有01,234个"):
            with self.subTest(text=text):
                self.assertEqual(plain_quantity(text), text)                      # 一个字不动
                self.assertEqual(split_quantity("白糖" + text), ("白糖", text))
                self.assertIsNone(parsed(text))


class FakeResult:
    def __init__(self, hit, texts=()):
        self.hit = hit
        self.filtered_results = [type("R", (), {"text": text, "score": .99})() for text in texts]
        self.all_results = self.filtered_results


class FakeContext:
    """仿 maa Context：按脚本逐帧给截图（用完停在最后一帧；cycle 时循环）；页签亮不亮看 (0,0) 像素；只识别节点按顺序吐文字。"""

    def __init__(self, frames, texts=(), ready=False, cycle=False):
        self.frames, self.texts, self.ready, self.cycle = list(frames), list(texts), ready, cycle
        self.canvases, self.tasks, self.nodes, self.shots = [], [], [], 0
        owner = self

        class Job:
            def __init__(self, image):
                self.image = image

            def wait(self):
                return self

            def get(self):
                return self.image

        class Controller:
            def post_screencap(self):
                owner.shots += 1
                if owner.cycle:
                    image = owner.frames[(owner.shots - 1) % len(owner.frames)]
                else:
                    image = owner.frames.pop(0) if len(owner.frames) > 1 else owner.frames[0]
                return Job(image)

        class Tasker:
            stopping = False
            controller = Controller()

        self.tasker = Tasker()

    def run_task(self, entry, override=None):
        self.tasks.append((entry, override))
        return type("D", (), {"nodes": []})()

    def run_recognition(self, node, image, override=None):
        self.nodes.append(node)
        if node == "Rec_BagScan_ConsTab_Lit_Clr":
            return FakeResult(bool(image[0, 0, 0] == 255))
        if node == "Rec_BagScan_Text_Rec":
            self.canvases.append(image.shape)
            return FakeResult(True, [self.texts.pop(0)] if self.texts else [])
        if node == "BagScan_Detail_Ready":
            return FakeResult(self.ready)
        return FakeResult(True, [])


def listed():
    image = popup()
    image[0, 0] = 255           # 页签亮：列表在、没有遮罩
    return image


class DeviceTests(unittest.TestCase):
    """MaaBagDevice 的点开与读：等出现动画走完、按颜色定字、拼读一次、读完核对；不成就退回原节点。"""

    ATTACH = {"name": [262, 183, 370, 51], "name_lit": [262, 183, 290, 51], "quantity": [265, 250, 280, 90],
              "close_icon": [992, 172, 32, 32], "open_timeout": 200, "timeout": 300, "interval": 0}

    def device(self, context):
        return import_bag_scan(self).MaaBagDevice(context, "ready", self.ATTACH)

    def test_first_open_waits_for_three_still_frames_then_keeps_the_close_icon(self):
        frames = [listed(), popup(slide=8, name_value=180), popup(slide=-2), popup(), popup(), popup(), popup()]
        context = FakeContext(frames, texts=["黄色史莱姆拥有2,876个"])
        device = self.device(context)
        reading = device.open_detail([678, 455, 94, 94])
        self.assertEqual((reading.names[0][0], reading.quantities[0][0], reading.settled),
                         ("黄色史莱姆", "拥有2876个", True))
        self.assertEqual(context.tasks[0], ("BagScan_Detail_Open", {"BagScan_Detail_Open": {"target": [678, 455, 94, 94]}}))
        self.assertEqual(len(context.canvases), 1)             # 名称与数量拼成一行，只识别一次
        self.assertIsNotNone(device.anchor)                    # 核对通过，记下终态的「×」

    def test_later_opens_settle_one_frame_after_the_close_icon_is_back(self):
        context = FakeContext([popup(), popup(), popup(), popup()], texts=["甲拥有1个", "乙拥有2个"])
        device = self.device(context)
        device.open_detail([577, 152, 94, 94])
        context.frames, context.shots = [listed(), popup(slide=-2), popup(), popup(), popup()], 0
        reading = device.open_detail([678, 152, 94, 94])
        self.assertEqual(reading.names[0][0], "乙")
        self.assertEqual(context.shots, 5)       # 列表、越过终点、到终点、再一帧确认静止、读完核对

    def test_name_only_reads_one_crop(self):
        context = FakeContext([popup()] * 4, texts=["毒蛇之手制作材料箱"])
        reading = self.device(context).peek_detail([577, 152, 94, 94])
        self.assertEqual((reading.names, reading.quantities, reading.settled), ([("毒蛇之手制作材料箱", .99)], [], True))

    def test_text_that_changes_after_reading_is_read_again(self):
        frames = [popup(), popup(), popup(), popup(name_value=230), popup(name_value=230)]
        context = FakeContext(frames, texts=["甲拥有1个", "甲拥有12个"])
        reading = self.device(context).open_detail([577, 152, 94, 94])
        self.assertEqual((reading.quantities[0][0], len(context.canvases)), ("拥有12个", 2))

    def test_popup_that_never_shows_is_not_opened(self):
        context = FakeContext([listed()])
        self.assertIsNone(self.device(context).open_detail([577, 152, 94, 94]))

    def test_fade_that_never_ends_falls_back_to_the_old_nodes(self):
        context = FakeContext([popup(slide=2), popup()], cycle=True)
        reading = self.device(context).open_detail([577, 152, 94, 94])
        self.assertFalse(reading.settled)
        self.assertIn("BagScan_Detail_Ready", context.nodes)
        self.assertNotIn("Rec_BagScan_Text_Rec", context.nodes)

    def test_reading_again_while_the_popup_is_gone_gives_nothing(self):
        device = self.device(FakeContext([listed()]))
        self.assertIsNone(device.read_detail())
        self.assertEqual(device.read_names(), [])


def corner_cell(*blocks, color=(255, 255, 255)):
    """一张 300×300 的图，格子在 [100, 100, 80, 80]；blocks 是格子下沿条带里的竖条 (相对格子左沿的 x, 宽)。"""
    image = np.full((300, 300, 3), 40, np.uint8)
    for left, width in blocks:
        image[100 + 63:100 + 75, 100 + left:100 + left + width] = color
    return image


class CornerTests(unittest.TestCase):
    """角标读数（用户 11-16，设计 §23）：与今天前一次一样才不点开；待定的数等段尾或列表底才定。"""

    NAMES = ["兽肉", "米", "白糖"]

    def bag(self, *items):
        return Bag(layout(*items, *fillers(10), *others(20), end=False))

    def test_corner_equal_to_the_last_reading_is_not_opened(self):
        held = amounts(*self.NAMES)
        device = Device(self.bag(*self.NAMES), held)
        device.corners = dict(held)
        result = run(device, self.NAMES, corner=device.corner, baseline={"兽肉": 100, "米": 101})
        self.assertEqual(device.opens, ["白糖"])                 # 没有前一次读数的照旧点开
        self.assertEqual(device.corner_reads, ["兽肉", "米"])
        self.assertEqual((result["quantities"], result["corner"]), (held, ["兽肉", "米"]))
        # 待查的都有着落了也不停：等食材段走完，确认没有别的格子，角标的数才定下来、写进存档
        self.assertEqual(result["termination"], "section_end")
        self.assertEqual(persisted(device), sorted(self.NAMES))
        self.assertEqual((result["cells"]["corner"], result["cells"]["opened"]), (2, 1))

    def test_corner_that_differs_is_opened(self):
        held = amounts(*self.NAMES)
        device = Device(self.bag(*self.NAMES), held)
        device.corners = {"兽肉": 100, "米": 1}                  # 米：角标读成别的数（截半、读错）
        result = run(device, self.NAMES, corner=device.corner, baseline={"兽肉": 100, "米": 101})
        self.assertEqual(device.opens, ["米", "白糖"])
        self.assertEqual(result["quantities"], held)
        self.assertEqual((result["corner"], result["cells"]["corner_missed"]), (["兽肉"], 1))

    def test_another_cell_of_the_same_item_is_opened_for_the_total(self):
        # 同一物品几格不一定挨着（11-11 模拟器：兽肉 45,192 与两格 99,999 中间隔着苹果、白糖、巧克力）
        held = {"兽肉": 100_100, "米": 101, "白糖": 102}
        device = Device(self.bag("兽肉", "米", "白糖", "兽肉"), held)
        device.corners = {"兽肉": 100, "米": 101}
        result = run(device, self.NAMES, corner=device.corner, baseline={"兽肉": 100, "米": 101})
        self.assertEqual(device.opens, ["白糖", "兽肉"])          # 又见到兽肉：点开读详情里的总数
        self.assertEqual(result["quantities"], held)
        self.assertEqual((result["corner"], result["cells"]["corner_reopened"]), (["米"], 1))

    def test_page_walked_again_after_an_unmeasurable_shift_does_not_reopen(self):
        # 11-16 PC 实机：点开没反应、列表挪了多少判不出，这一页从头重走；角标收下的那几格又遇到一次，
        # 曾被当成「同一物品的别的格」逐个点开（吸取结束那次白点了 33 格）。重走时先重读角标，一样就是原来那格。
        late = ["鸡蛋", "小麦", "番茄"]
        held = amounts("兽肉", "米", "白糖", *late, "盐")
        device = Device(Bag(layout("兽肉", "米", "白糖", *fillers(34), *late, "盐")), held, drifts={"盐": -150})
        device.corners = dict(held)
        result = run(device, list(held), corner=device.corner, baseline={name: held[name] for name in late})
        self.assertEqual(result["cells"]["resplit"], 1)
        self.assertEqual(result["cells"]["corner_reopened"], 0)
        self.assertFalse(set(late) & set(device.opens))
        self.assertEqual(device.corner_reads.count("鸡蛋"), 2)       # 收下时读一次，重走时再读一次
        self.assertEqual((result["quantities"], result["corner"]), (held, sorted(late)))

    def test_scan_that_ends_early_drops_the_corner_reads(self):
        held = amounts(*self.NAMES)
        device = Device(self.bag(*self.NAMES), held, stop_after_opens=1)
        device.corners = dict(held)
        result = run(device, self.NAMES, corner=device.corner, baseline={"兽肉": 100, "米": 101})
        self.assertEqual(result["termination"], "stopped")
        self.assertEqual({name: result["unknown"][name] for name in ("兽肉", "米")},
                         {"兽肉": "corner_unconfirmed", "米": "corner_unconfirmed"})
        self.assertNotIn("兽肉", result["quantities"])
        self.assertNotIn("兽肉", persisted(device))

    def test_full_stacks_tickets_shared_icons_and_no_baseline_are_opened(self):
        cases = {
            "前一次正好满格": ({"兽肉": 99_999}, {}),
            "一格上限 99": ({"兽肉": 99}, {}),
            "券（图标信不过）": ({"券甲": 5}, {"kinds": {"券甲": "券"}}),
            "共用图标": ({"冰河魔石": 3}, {"sees": {"冰河魔石": ("冰河魔石", "忍耐精髓")}}),
            "启动后第一次（没有前一次）": ({}, {}),
        }
        for label, (baseline, extra) in cases.items():
            with self.subTest(label):
                name = next(iter(baseline), "兽肉")
                device = Device(self.bag(name), {name: baseline.get(name, 7)}, **extra)
                device.corners = {name: baseline.get(name, 7)}
                options = {"loose_kinds": {"券"}, "loose_names": TICKETS} if "kinds" in extra else {}
                result = run(device, [name], corner=device.corner, baseline=baseline, **options)
                self.assertEqual(device.corner_reads, [])
                self.assertEqual(device.opens, [name])
                self.assertEqual(result["corner"], [])

    def test_corner_box_follows_the_white_digits_from_the_right(self):
        layout = CornerLayout()
        self.assertEqual(corner_box(corner_cell((60, 4), (67, 4), (74, 4)), [100, 100, 80, 80], layout),
                         [157, 160, 23, 18])       # 右边留白到格子右沿为止
        # 「1」和后一位隔了 7 列（11-16 模拟器截图）：仍在同一串里
        box = corner_box(corner_cell((51, 3), (60, 4), (67, 4), (74, 4)), [100, 100, 80, 80], layout)
        self.assertEqual(box[0], 148)

    def test_corner_box_gives_up_when_unsure(self):
        layout = CornerLayout()
        for label, image in (("左边隔着一段还有白字", corner_cell((40, 3), (60, 4), (67, 4), (74, 4))),
                             ("白色离右沿太远，是图标", corner_cell((10, 10))),
                             ("有颜色的不是数量字", corner_cell((60, 4), (67, 4), (74, 4), color=(0, 0, 255))),
                             ("没有字", corner_cell())):
            with self.subTest(label):
                self.assertIsNone(corner_box(image, [100, 100, 80, 80], layout))

    def test_corner_text_must_be_a_well_grouped_number(self):
        for texts, value in ((["52"], 52), (["1,200"], 1200), (["1.200"], 1200), (["1，200"], 1200),
                             (["45,192"], 45192), (["1,234,567"], 1234567), (["1,200", "1,200"], 1200),
                             (["1200"], None), (["05"], None), ([",992"], None), (["1.1M"], None),
                             (["1:390"], None), (["12,34"], None), (["1,200", "200"], None), ([], None)):
            with self.subTest(texts=texts):
                self.assertEqual(parse_corner(texts), value)

    def test_both_packs_carry_the_corner_settings(self):
        for pack_name in ("base", "pc"):
            with self.subTest(pack=pack_name):
                attach = pipeline(pack_name)["Agt_BagScan_Grid"]["attach"]["corner"]
                self.assertEqual(CornerLayout.from_attach(attach), CornerLayout())   # 11-16 两种截图都按这组测过

    def test_device_reads_the_corner_crop_once(self):
        image = corner_cell((60, 4), (67, 4), (74, 4))
        context = FakeContext([image], texts=["1,200", "1200"])
        device = import_bag_scan(self).MaaBagDevice(context, "ready", DeviceTests.ATTACH)
        self.assertEqual(device.read_corner(image, [100, 100, 80, 80]), 1200)
        self.assertEqual(context.canvases, [(18, 23, 3)])         # 只识别裁下来的那一小块
        self.assertIsNone(device.read_corner(image, [100, 100, 80, 80]))
        self.assertIsNone(device.read_corner(corner_cell(), [100, 100, 80, 80]))
        self.assertEqual(len(context.canvases), 2)                # 找不到字就不送识别


class ShiftTests(unittest.TestCase):
    def setUp(self):
        self.bag = Bag(layout(*fillers(120)))

    def grid(self):
        x, y, w, h = GRID
        return self.bag.screen()[y:y + h, x:x + w]

    def test_same_moved_and_too_far(self):
        before = self.grid()
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[1], "same")
        self.bag.offset = 440
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[:2], (440, "moved"))
        self.bag.offset = 560
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[1], "broken")

    def test_fixed_footer_is_ignored(self):
        # 10-28 实机：列表区最下 64 行是固定底栏。底栏不能让「真动了」变成「接不上」或「没动」。
        self.bag = Bag(layout(*fillers(120)), footer=64)
        before = self.grid()
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[1], "same")
        self.bag.offset = 440
        shift, state, info = estimate_shift(before, self.grid(), ROW, 12.0)
        self.assertEqual((shift, state), (440, "moved"))
        self.assertEqual(info["trimmed"][1], 64)
        # 可视高度只剩 570：滚 500 已超过「可视高度减一行」，必须报接不上。
        self.bag.offset = 500
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[1], "broken")
        self.bag.offset = 40
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[:2], (40, "moved"))

    def test_identical_rows_resolved_by_taller_strip(self):
        # 11-13 PC 实机：上一页底部是一整行同款物品，新页里这样的行连着两行，一行高的条带在 440 与 340 两处一样好。
        # 加高到两行带上上面那行不同的内容，就只剩真位置。
        self.bag = Bag(layout(*fillers(30), *["同款"] * 18, *fillers(30, 30)))
        before = self.grid()
        self.bag.offset = 440
        shift, state, info = estimate_shift(before, self.grid(), ROW, 12.0)
        self.assertEqual((shift, state), (440, "moved"))
        self.assertLess(info["second"] - info["best"], 4.0)     # 一行高时确实分不开
        self.assertIn("widened", info)

    def test_identical_rows_beyond_two_rows_stay_broken(self):
        # 加高到两行仍全是同款：画面给不出证据，照旧判接不上，不猜。
        self.bag = Bag(layout(*fillers(18), *["同款"] * 36, *fillers(30, 18)))
        before = self.grid()
        self.bag.offset = 440
        self.assertEqual(estimate_shift(before, self.grid(), ROW, 12.0)[1], "broken")

    def test_identical_rows_near_search_limit_stay_broken(self):
        # 滚得太远，加高后的条带够不着真位置（520）：不能退而采信够得着的那个假位置（420），照旧判接不上。
        self.bag = Bag(layout(*fillers(30), *["同款"] * 18, *fillers(30, 30)))
        before = self.grid()
        self.bag.offset = 520
        shift, state, info = estimate_shift(before, self.grid(), ROW, 12.0)
        self.assertEqual(state, "broken")
        self.assertLess(info["widened"]["tall"], ROW + ROW // 4)


def pipeline(pack):
    return json.loads((ROOT / f"assets/resource/{pack}/pipeline/BagScan.json").read_text(encoding="utf-8"))


def grid_of(pack):
    attach = pipeline(pack)["Agt_BagScan_Grid"]["attach"]
    return Grid(area=tuple(attach["area"]), columns=attach["columns"], left=attach["left"], pitch=tuple(attach["pitch"]),
                footer=attach["footer"], row_offset=attach["row_offset"], known=attach["known"], margin=attach["margin"])


class CellTests(unittest.TestCase):
    """整页读图：图集与物品表对得上；拿图集里的图标拼一页，能原样读回来。"""

    def compose(self, pack_name, names, drop=0, blanks=0):
        """按资源包的排布拼一页：先画格子底板，再把图标的有效像素贴进去。drop：图标比格子上沿估计多往下几像素。"""
        pack, grid = icon_pack(pack_name), grid_of(pack_name)
        table = item_table()["packs"][pack_name]
        stems = {(item["cn"] if item["profile"] != "other" else "#" + item["cn"]): item["icon"]
                 for item in reversed(item_table()["items"]) if item["icon"]}
        index = {name: table["icons"].index(stem) for name, stem in stems.items() if stem in table["icons"]}
        with __import__("PIL.Image", fromlist=["Image"]).open(ROOT / "agent/data" / table["atlas"]) as image:
            atlas = np.asarray(image.convert("RGB"))
        height, width = pack.shape
        frame = np.full((720, 1280, 3), 18, np.uint8)
        cells = list(names) + [None] * blanks
        for number, name in enumerate(cells):
            row, col = divmod(number, grid.columns)
            left = int(round(grid.left + col * grid.pitch[0]))
            top = grid.area[1] + 12 + int(round(row * grid.pitch[1]))
            frame[top:top + pack.cell[1], left:left + pack.cell[0]] = (58, 50, 44)
            if name is None:
                continue
            r, c = divmod(index[name], table["columns"])
            tile = atlas[r * height:(r + 1) * height, c * width:(c + 1) * width]
            mask = ~(tile == (0, 255, 0)).all(axis=2)
            y = top + pack.band[0] + grid.row_offset + drop
            frame[y:y + height, left:left + width][mask] = tile[mask][:, ::-1]
            frame[top + pack.cell[1] - 16:top + pack.cell[1] - 6, left + width - 30:left + width - 8] = 255   # 数量字
        return read_page(frame, grid, pack)

    def test_icon_packs_match_the_item_table(self):
        table = item_table()
        catalog = [item for item in table["items"] if item["profile"] != "other"]
        self.assertEqual(len(catalog), len({item["cn"] for item in catalog}))
        for pack_name in ("base", "pc"):
            with self.subTest(pack=pack_name):
                pack = icon_pack(pack_name)
                self.assertGreater(len(pack.icons), 300)
                names = {name for icon in pack.icons for name in icon.names}
                # 每个带图标的目录物品都能在图集里找到自己（有效像素不足被弃用的除外）。
                missing = [item["cn"] for item in catalog if item["icon"] in table["packs"][pack_name]["icons"]
                           and item["cn"] not in names]
                self.assertEqual(missing, [])
                self.assertTrue(all(name.startswith("#") or icon.profiles - {"other"}
                                    for icon in pack.icons for name in icon.names))

    def test_a_composed_page_is_read_back(self):
        names = ["兽肉", "盐", "金矿石", "铜矿石", "铁矿石", "初级天赋技能书", "#达非的灵狐珠", "远古水晶"]
        for pack_name, drop in (("base", 0), ("base", 3), ("pc", 0), ("pc", -4)):
            with self.subTest(pack=pack_name, drop=drop):
                page = self.compose(pack_name, names, drop=drop, blanks=10)
                found = [cell.icon.names[0] if cell.icon else None for cell in page.cells if not cell.empty]
                self.assertEqual(found, names)        # 矿石三种形状几乎一样，靠颜色核分开
                self.assertTrue(page.ends_with_blanks())
                self.assertTrue(all(cell.empty for cell in page.cells[len(names):]))

    def test_row_tops_are_found_when_the_quantity_text_steals_the_phase(self):
        # 实机截图（tools/fixtures/bag_scan_misaligned）：估出的行上沿比真实位置高 14 像素，旧对齐只找 ±7 像素够不回来。
        with __import__("PIL.Image", fromlist=["Image"]).open(ROOT / "tools/fixtures/bag_scan_misaligned/page.jpg") as image:
            frame = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        page = read_page(frame, grid_of("base"), icon_pack("base"))
        self.assertTrue(page.aligned)
        self.assertEqual(len(page.tops), 5)
        self.assertLessEqual(abs(page.tops[1] - 227), 2)
        egg = next(cell for cell in page.cells if cell.row == 1 and cell.col == 5)
        self.assertEqual(egg.icon.names, ("鸡蛋",))
        self.assertLessEqual(sum(cell.icon is None for cell in page.occupied()), 2)

    def test_shared_pictures_are_merged_into_one_icon(self):
        pack = icon_pack("base")
        shared = {icon.names for icon in pack.icons if len(icon.names) > 1}
        self.assertIn(("精炼石", "五色魔石"), {tuple(sorted(names, key=["精炼石", "五色魔石"].index))
                                         for names in shared if set(names) == {"精炼石", "五色魔石"}})

    def test_hsv_follows_the_opencv_convention(self):
        pixels = np.array([[[0, 0, 255], [0, 255, 0], [255, 0, 0], [128, 128, 128], [40, 120, 200]]], np.uint8)
        self.assertEqual(hsv(pixels)[0].tolist(),
                         [[0, 255, 255], [60, 255, 255], [120, 255, 255], [0, 0, 128], [15, 204, 200]])
        self.assertEqual(color_count(pixels, [10, 100, 100], [20, 255, 255]), 1)


def import_bag_scan(test):
    """BagScan 动作模块依赖 maa 包；没有就跳过这几条，引擎测试照常跑。

    action 包导入期会向 Agent 注册回调，离线没有 Agent 客户端：导入时屏蔽这些注册
    （与 priv/tools/run_offline_test.py 同一手法），本文件因此仍可直接运行。
    """
    try:
        from maa.library import Library
        Library.version()
        from maa.agent.agent_server import AgentServer
    except Exception as exc:  # 本机没装 MaaFw
        test.skipTest(f"maa 不可用：{exc}")
    noop = lambda *args, **kwargs: None  # noqa: E731
    with patch.object(AgentServer, "custom_action", return_value=lambda cls: cls), \
         patch.object(AgentServer, "custom_recognition", return_value=lambda cls: cls), \
         patch.object(AgentServer, "_set_api_properties", noop), \
         patch.object(AgentServer, "add_tasker_sink", noop), \
         patch.object(AgentServer, "add_context_sink", noop), \
         patch.object(AgentServer, "add_resource_sink", noop), \
         patch.object(AgentServer, "add_controller_sink", noop):
        from action import bag_scan
    return bag_scan


class ConfigTests(unittest.TestCase):
    """节点里的扫描类型与排布，以及背包读名用的名录。"""

    def test_scopes_turn_into_sections(self):
        bag_scan = import_bag_scan(self)
        config = pipeline("base")["Agt_BagScan_Config"]["attach"]
        names, sections = bag_scan._plan(config, bag_scan._scope_keys("A", config["scopes"]))
        self.assertEqual([(section.kind, section.evidence) for section in sections], [("head", "ingredient_boundary")])
        self.assertIn("兽肉", names)
        self.assertIn("香草牛排", names)
        self.assertNotIn("铁矿石", names)
        names, sections = bag_scan._plan(config, bag_scan._scope_keys(["A", "B", "C"], config["scopes"]))
        self.assertEqual([section.kind for section in sections], ["head", "run"])      # B 没有段尾，扫到底
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({"初级天赋技能书", "铁矿石", "Happy Halloween！幸运抽抽乐券"} <= set(names))
        for bad in ("Z", [], ["A", 3]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bag_scan._scope_keys(bad, config["scopes"])

    def test_each_pack_grid_points_at_its_own_icons(self):
        bag_scan = import_bag_scan(self)
        packs = item_table()["packs"]
        for pack_name in ("base", "pc"):
            with self.subTest(pack=pack_name):
                attach = pipeline(pack_name)["Agt_BagScan_Grid"]["attach"]
                self.assertEqual(attach["icons"], pack_name)
                grid = bag_scan._grid(attach)
                cell = packs[pack_name]["cell"]
                self.assertLessEqual(grid.left + (grid.columns - 1) * grid.pitch[0] + cell[0],
                                     grid.area[0] + grid.area[2] + 2)
                self.assertGreater(grid.pitch[1], cell[1])

    def test_detail_opens_by_click_only_and_closes_with_one_click(self):
        """点开只点击（等弹窗走完由 py 截帧判定）；关闭只点一下：连点时后两下会让紧接着的点开被游戏吞掉（11-12 实测）。"""
        nodes = pipeline("base")
        self.assertNotIn("next", nodes["BagScan_Detail_Open"])
        self.assertEqual(nodes["BagScan_Detail_Close"].get("repeat", 1), 1)
        self.assertNotIn("repeat", pipeline("pc").get("BagScan_Detail_Close", {}))
        self.assertTrue(nodes["Rec_BagScan_Text_Rec"]["only_rec"])

    def test_bag_names_cover_both_scripts_and_flag_shared_names(self):
        bag_scan = import_bag_scan(self)
        from utils.ocr_item_name import resolve_ocr_name
        aliases, ambiguous = bag_scan._names()
        # 「光圣石」是游戏物品表的简体原名，项目规范名是「光明圣石」：读到原名也要归到规范名，否则永远对不上、被误判 0。
        for text, name in (("煉製水晶", "精炼水晶"), ("暴走火種", "狂暴火种"), ("女神的眼淚", "女神之泪"),
                           ("鮭魚", "三文鱼"), ("精炼石", "精炼石"), ("光圣石", "光明圣石"), ("暗的聖石", "黑暗圣石"),
                           ("冰龙鳞", "#冰龙鳞"), ("純白牛奶", "#牛奶"), ("牛奶", "牛奶")):
            with self.subTest(text=text):
                self.assertEqual(resolve_ocr_name(text, .99, aliases=aliases)["name"], name)
        self.assertIn("牛奶", ambiguous["cn"])           # 简体里食材与一个活动道具同名
        self.assertNotIn("牛奶", ambiguous["tw"])        # 繁体里那个活动道具叫「純白牛奶」，「牛奶」只会是食材（11-15 PC 实机）
        self.assertNotIn("兽肉", ambiguous["cn"] | ambiguous["tw"])
        self.assertNotIn("精炼水晶", set(item_aliases().values()))     # 共用名录没被动过


class ActionTests(unittest.TestCase):
    def test_all_targets_follow_the_scope_and_keep_items_without_icons(self):
        bag_scan = import_bag_scan(self)
        names = bag_scan.profile_names("growth")
        self.assertEqual(bag_scan._targets("all", names), names)
        self.assertIn("Happy Halloween！服装选择券", names)       # 没有图标的也盘：点开读名
        self.assertEqual(bag_scan._targets([" 兽肉 ", "兽肉", "馬鈴薯"], names), ["兽肉", "土豆"])

    def test_unwritten_zeroes_become_unknown(self):
        bag_scan = import_bag_scan(self)
        for outcome in (False, RuntimeError("存档只读")):
            with self.subTest(outcome=outcome):
                record = {"absent": ["盐", "肉桂"], "unknown": {"胡椒": "detail_unreadable"}}
                mock = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(bag_scan, "set_inventory_quantities", **mock), self.assertRaises(RuntimeError):
                    bag_scan._save_absent(record, {"scan_id": "s1"},
                                          {"ingredient_boundary": ["盐"], "list_bottom": ["肉桂"]})
                self.assertEqual(record["absent"], [])
                self.assertEqual(record["unknown"], {"胡椒": "detail_unreadable", "盐": "persist_failed",
                                                     "肉桂": "persist_failed"})

    def test_zeroes_are_written_per_evidence(self):
        bag_scan = import_bag_scan(self)
        record = {"absent": ["盐", "肉桂"], "unknown": {}}
        with patch.object(bag_scan, "set_inventory_quantities", return_value=True) as write:
            bag_scan._save_absent(record, {"scan_id": "s1"}, {"ingredient_boundary": ["盐"], "list_bottom": ["肉桂"]})
        self.assertEqual([(call.args[0], call.args[2]["evidence"]) for call in write.call_args_list],
                         [({"盐": 0}, "ingredient_boundary"), ({"肉桂": 0}, "list_bottom")])
        self.assertEqual(record["absent"], ["盐", "肉桂"])

    def test_unknown_names_are_listed_by_reason(self):
        bag_scan = import_bag_scan(self)
        text = bag_scan.unknown_text({"灵魂鲜奶油": "cell_unverified", "胡椒": "detail_unreadable",
                                      "能量蘑菇": "cell_unverified"})
        self.assertEqual(text, "有格子没核实：灵魂鲜奶油、能量蘑菇；详情未核实：胡椒")


class MemoryAccountStore:
    data = {}

    @classmethod
    def load(cls):
        return copy.deepcopy(cls.data)

    @classmethod
    def save(cls, data):
        cls.data = copy.deepcopy(data)
        return True


class ScanRecordTests(unittest.TestCase):
    def setUp(self):
        MemoryAccountStore.data = {}
        patcher = patch.object(store, "PersistentStore", MemoryAccountStore)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def record(label="地图吸取前", **extra):
        return {"scan_id": "s1", "label": label, "started_at": "2026-10-28T01:00:00+00:00",
                "finished_at": "2026-10-28T01:02:00+00:00", "job_id": 7, "scope": ["A", "C"],
                "target_count": 3, "complete": False, "termination": "section_end", "continuity": "verified",
                "pages": 4, "quantities": {"兽肉": 5}, "absent": ["盐"], "unknown": {"胡椒": "detail_unreadable"},
                "cells": {"opened": 2}, "timing_ms": {"total": 1000}, **extra}

    def test_record_goes_to_events_and_latest_summary_by_label(self):
        saved = inventory_store.save_scan_record(self.record())
        self.assertEqual(saved["seq"], 1)
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        event = inventory["events"][-1]
        self.assertEqual((event["kind"], event["label"], event["quantities"]), ("inventory_scan", "地图吸取前", {"兽肉": 5}))
        self.assertEqual((event["scope"], event["cells"]), (["A", "C"], {"opened": 2}))    # 扫了哪几类要进存档
        self.assertEqual(event["item_facts"], {})
        summary = inventory["latest"]["scans"]["地图吸取前"]
        self.assertEqual((summary["read_count"], summary["absent_count"], summary["unknown_item_names"]),
                         (1, 1, ["胡椒"]))
        self.assertEqual(inventory_store.get_latest_scan("地图吸取前")["scan_id"], "s1")

    def test_sequence_increases_and_labels_do_not_overwrite_each_other(self):
        inventory_store.save_scan_record(self.record())
        second = inventory_store.save_scan_record(self.record(label="地图吸取后", scan_id="s2"))
        self.assertEqual(second["seq"], 2)
        scans = MemoryAccountStore.data["arbitrage"]["inventory"]["latest"]["scans"]
        self.assertEqual(sorted(scans), ["地图吸取前", "地图吸取后"])

    def test_legacy_bag_summary_is_untouched(self):
        MemoryAccountStore.data = {"arbitrage": {"inventory": {"latest": {"bag": {"run_id": "old"}}}}}
        inventory_store.save_scan_record(self.record())
        self.assertEqual(MemoryAccountStore.data["arbitrage"]["inventory"]["latest"]["bag"], {"run_id": "old"})

    def test_page_writes_use_the_shared_quantity_entry(self):
        self.assertTrue(inventory_store.set_inventory_quantities({"兽肉": 5}, "bag_detail", {"scan_id": "s1"}))
        fact = MemoryAccountStore.data["arbitrage"]["inventory"]["items"]["兽肉"]
        self.assertEqual((fact["quantity"], fact["quantity_source"]), (5, "bag_detail"))


class DayReadsTests(unittest.TestCase):
    """角标比的「前一次」：本进程、同账号、同一游戏日最近一次读到的数，而且实时库存还是仓检写的那个数。"""

    def setUp(self):
        MemoryAccountStore.data = {}
        patcher = patch.object(store, "PersistentStore", MemoryAccountStore)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bag_scan = import_bag_scan(self)
        self.bag_scan._DAY_READS.clear()
        self.addCleanup(self.bag_scan._DAY_READS.clear)

    def scanned(self, day, quantities, absent=(), unknown=()):
        inventory_store.set_inventory_quantities(quantities, "bag_detail", {"scan_id": "s"})
        self.bag_scan._keep_reads("a", day, {"quantities": quantities, "absent": list(absent),
                                             "unknown": dict.fromkeys(unknown, "detail_unreadable")})

    def test_only_same_day_same_account_and_untouched_reads_count(self):
        day = "2026-11-16"
        self.scanned(day, {"兽肉": 5, "米": 7, "盐": 3, "白糖": 9})
        self.assertEqual(self.bag_scan._baseline("a", day), {"兽肉": 5, "米": 7, "盐": 3, "白糖": 9})
        self.assertEqual(self.bag_scan._baseline("a", "2026-11-17"), {})
        self.assertEqual(self.bag_scan._baseline("b", day), {})
        store.set_inventory_quantities({"米": 4}, "sale_confirmed")      # 之后卖过：不再是仓检写的数
        self.scanned(day, {"兽肉": 6}, absent=["盐"], unknown=["白糖"])  # 这次判 0、没读准的不算前一次
        self.assertEqual(self.bag_scan._baseline("a", day), {"兽肉": 6})

    def test_new_day_starts_empty(self):
        self.scanned("2026-11-16", {"兽肉": 5})
        self.scanned("2026-11-17", {"米": 7})
        self.assertEqual(self.bag_scan._DAY_READS["a"], {"day": "2026-11-17", "values": {"米": 7}})

    def test_game_day_is_the_utc_date(self):
        self.assertEqual(self.bag_scan._game_day("2026-11-16T23:59:59+00:00"), "2026-11-16")


if __name__ == "__main__":
    unittest.main()
