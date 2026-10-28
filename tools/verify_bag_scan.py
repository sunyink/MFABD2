"""通用背包仓检回归：按页扫描、判 0 前提、两帧复核、恢复与停止；扫描记录存档。

全程合成画面与内存存档，不连接游戏、不读写用户真实存档。直接运行：
    python -B tools/verify_bag_scan.py -v
"""

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from utils import arbitrage_store as store
from utils import inventory_store
from utils.bag_scan_engine import BagScanEngine, Reading, estimate_shift


GRID = [578, 62, 600, 634]
ROW = 100
END = "初级天赋技能书"
INGREDIENTS = {"兽肉", "香草", "盐", "黄油", "胡椒", "土豆", "米", "白糖", "鸡蛋", "小麦", "牛奶", "番茄", "蘑菇",
               "洋葱", "大蒜", "辣椒", "苹果", "葡萄", "蜂蜜", "杏仁", "芥末", "料酒", "肉桂", "樱桃"}
QUANTITY_BOX = [270, 263, 90, 24]


def fillers(count, start=0):
    """填充格：既不是目标也不是末端，只用来把列表拉长。"""
    return [f"杂物{start + index:03d}" for index in range(count)]


def layout(*items, end=True):
    items = list(items) + ([END, "初级突破石"] if end else [])
    return [items[i:i + 6] + [None] * (6 - len(items[i:i + 6])) for i in range(0, len(items), 6)]


class Bag:
    """合成的背包列表：每行 6 格、行距 100，每个物品一块固定的随机纹理，滚动就是改视窗偏移。"""

    def __init__(self, rows, swipe=440, footer=0, toast_frames=0):
        self.rows = rows
        self.swipe = swipe
        self.offset = 0
        # footer：压在列表区最下方、不随滚动的界面（实机模拟器约 64 行），列表留出同样的底部余量。
        self.footer = footer
        # toast_frames：前几次截图里底栏上半截叠着会消失的提示（10-28 第三轮试跑：底栏只有 17 行两帧相同）。
        self.toast_frames = toast_frames
        self.footer_pixels = np.random.default_rng(99).integers(0, 255, (footer, GRID[2], 3), np.uint8)
        height = len(rows) * ROW
        self.max_offset = max(0, height + footer - GRID[3])
        self.canvas = np.full((max(height + footer, GRID[3]), GRID[2], 3), 40, np.uint8)
        textures = {}
        for r, row in enumerate(rows):
            for c, item in enumerate(row):
                if item is None:
                    continue
                if item not in textures:
                    seed = sum(map(ord, item)) * 7919 + len(textures)
                    textures[item] = np.random.default_rng(seed).integers(0, 255, (80, 80, 3), np.uint8)
                self.canvas[r * ROW + 10:r * ROW + 90, c * 100 + 10:c * 100 + 90] = textures[item]

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

    def cells(self):
        """视窗里完整可见（没被底栏挡住）的格子：(物品, 屏幕上的框)。"""
        for r, row in enumerate(self.rows):
            top = r * ROW + 10 - self.offset
            if top < 0 or top + 80 > GRID[3] - self.footer:
                continue
            for c, item in enumerate(row):
                if item is not None:
                    yield item, [GRID[0] + c * 100 + 10, GRID[1] + top, 80, 80]

    def item_at(self, box):
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        for item, cell in self.cells():
            if cell[0] <= cx < cell[0] + cell[2] and cell[1] <= cy < cell[1] + cell[3]:
                return item
        return None


class Device:
    def __init__(self, bag, quantities, *, decoys=None, end_decoys=(), frames=None, list_faults=0,
                 stop_after_opens=None, open_failures=None, close_failures=0):
        self.bag = bag
        self.quantities = quantities
        self.decoys = decoys or {}          # 目标名 → 会被它的模板误命中的其他物品
        self.end_decoys = set(end_decoys)   # 被误当成末端标志的物品
        self.frames = frames or {}          # 物品 → 依次出现的数量帧 [(文字, 框)]
        self.list_faults = list_faults
        self.stop_after_opens = stop_after_opens
        self.open_failures = dict(open_failures or {})
        self.close_failures = close_failures
        self.opens = []
        self.current = None
        self.frame_iter = None
        self.last = None
        self.persisted = []
        self.swipes = 0
        self.resets = 0

    def stopping(self):
        return self.stop_after_opens is not None and len(self.opens) >= self.stop_after_opens

    def capture(self):
        return self.bag.screen()

    def list_ready(self, image):
        if self.list_faults:
            self.list_faults -= 1
            return False
        return self.current is None

    def match(self, template, image):
        name = template.removeprefix("tpl/")
        found = [(box, .99) for item, box in self.bag.cells() if item == name]
        found += [(box, .95) for item, box in self.bag.cells() if item in self.decoys.get(name, ())]
        return found

    def end_markers(self, image):
        return [box for item, box in self.bag.cells() if item == END or item in self.end_decoys]

    def _frame(self, item):
        if self.frame_iter is not None:
            self.last = next(self.frame_iter, self.last)
            text, box = self.last
            return Reading(names=[(item, .99)], quantities=[(text, .99, list(box))])
        return Reading(names=[(item, .99)],
                       quantities=[(f"拥有{self.quantities.get(item, 0)}个", .99, list(QUANTITY_BOX))])

    def open_detail(self, box):
        item = self.bag.item_at(box)
        if self.open_failures.get(item, 0):
            self.open_failures[item] -= 1
            return None
        self.opens.append(item)
        self.current = item
        sequence = self.frames.get(item)
        self.frame_iter = iter(sequence) if sequence else None
        self.last = sequence[-1] if sequence else None
        return self._frame(item)

    def read_detail(self):
        return self._frame(self.current) if self.current else None

    def read_names(self):
        return [(self.current, .99)] if self.current else []

    def read_quantity_scaled(self):
        return [f"拥有{self.quantities.get(self.current, 0)}个"]

    def close_detail(self):
        if self.close_failures:
            self.close_failures -= 1
            return False
        self.current = None
        return True

    def next_page(self):
        """仿 SmartAction：画面变了报 moved，连续滑不动报 bottom。"""
        self.swipes += 1
        before = self.bag.offset
        self.bag.offset = min(self.bag.max_offset, before + self.bag.swipe)
        return "moved" if self.bag.offset != before else "bottom"

    def reset_to_top(self):
        self.resets += 1
        self.current = None
        self.bag.offset = 0
        return True


def run(device, targets, **options):
    catalog = {name: f"tpl/{name}" for name in INGREDIENTS}
    engine = BagScanEngine(device, catalog, targets, grid_roi=GRID, row_height=ROW,
                           ingredient_names=INGREDIENTS,
                           on_page=lambda page, reads: device.persisted.append((page, reads)), **options)
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
        self.assertEqual((result["termination"], result["pages"], device.swipes), ("all_found", 1, 0))
        self.assertEqual(result["quantities"], amounts(*names))
        self.assertTrue(result["complete"])

    def test_one_pass_reads_late_items_and_zeroes_missing_after_verified_end(self):
        bag = Bag(layout("兽肉", *fillers(40), "黄油", *fillers(30, 40), "芥末"))
        device = Device(bag, amounts("兽肉", "黄油", "芥末"))
        result = run(device, ["盐", "黄油", "芥末", "肉桂", "兽肉"])
        self.assertEqual((result["termination"], result["zero_evidence"]), ("section_end", "ingredient_boundary"))
        self.assertEqual(result["quantities"], amounts("兽肉", "黄油", "芥末"))
        self.assertEqual(result["absent"], ["盐", "肉桂"])
        self.assertEqual(result["continuity"], "verified")
        self.assertGreater(result["pages"], 1)
        self.assertEqual(device.resets, 0)
        self.assertEqual(device.opens.count(END), 1)
        self.assertEqual(persisted(device), ["兽肉", "芥末", "黄油"])

    def test_end_marker_on_an_ingredient_is_not_the_end(self):
        bag = Bag(layout("土豆", *fillers(30), "胡椒"))
        device = Device(bag, amounts("土豆", "胡椒"), end_decoys={"土豆"})
        result = run(device, ["盐", "胡椒"])
        self.assertIn("土豆", device.opens)
        self.assertEqual(result["termination"], "section_end")
        # 土豆只是被点开核名，没有被请求，不记读数。
        self.assertEqual(result["quantities"], {"胡椒": amounts("土豆", "胡椒")["胡椒"]})
        self.assertEqual(result["absent"], ["盐"])

    def test_list_bottom_without_growth_items_still_concludes(self):
        bag = Bag(layout(*fillers(50), "胡椒", end=False))
        device = Device(bag, {"胡椒": 7})
        result = run(device, ["盐", "胡椒"])
        self.assertEqual((result["termination"], result["zero_evidence"]), ("list_bottom", "list_bottom"))
        self.assertEqual(result["quantities"], {"胡椒": 7})
        self.assertEqual(result["absent"], ["盐"])
        self.assertTrue(result["complete"])

    def test_fixed_footer_run_reaches_real_bottom_and_concludes(self):
        # 10-28 试跑：底栏让每页都报接不上，最后一页又被误判成没动、提前收尾，4 项缺货没能判 0。
        names = ["兽肉", "米", "白糖", "鸡蛋", "胡椒"]
        bag = Bag(layout("兽肉", *fillers(40), "米", *fillers(40, 40), "白糖", "鸡蛋", *fillers(30, 80), "胡椒",
                         end=False), footer=64)
        device = Device(bag, amounts(*names))
        result = run(device, [*names, "盐", "肉桂"])
        self.assertEqual(result["continuity"], "verified")
        self.assertEqual(result["termination"], "list_bottom")
        self.assertEqual(bag.offset, bag.max_offset)
        self.assertEqual(result["quantities"], amounts(*names))
        self.assertEqual(result["absent"], ["盐", "肉桂"])

    def test_changing_footer_needs_fixed_footer_rows(self):
        # 底栏内容在第一页时不同（提示没消失），自动剔除只剔得掉最下 17 行：不给底栏行数会报接不上，给 64 行就正常。
        def scan(**options):
            bag = Bag(layout("兽肉", *fillers(60), "胡椒", end=False), footer=64, toast_frames=1)
            return run(Device(bag, amounts("兽肉", "胡椒")), ["兽肉", "胡椒", "盐"], **options)

        self.assertEqual(scan()["continuity"], "broken")
        result = scan(footer_rows=64)
        self.assertEqual((result["continuity"], result["absent"]), ("verified", ["盐"]))

    def test_template_hit_on_another_item_blocks_zero(self):
        bag = Bag(layout("米", *fillers(30)))
        device = Device(bag, {"米": 9}, decoys={"盐": ("米",)})
        result = run(device, ["盐", "米"])
        self.assertEqual(result["quantities"], {"米": 9})
        self.assertEqual(result["unknown"], {"盐": "detail_unreadable"})
        self.assertEqual(result["absent"], [])

    def test_skipped_rows_never_produce_zero(self):
        # 第 7 行在两页之间被整行跳过：盐其实在背包里，绝不能判 0。
        bag = Bag(layout(*fillers(36), "盐", *fillers(5, 36), "米", *fillers(40, 41)), swipe=700)
        device = Device(bag, {"盐": 3, "米": 4})
        result = run(device, ["盐", "米", "肉桂"])
        self.assertEqual(result["continuity"], "broken")
        self.assertEqual(result["quantities"], {"米": 4})
        self.assertEqual(result["absent"], [])
        self.assertEqual(result["unknown"], {"盐": "continuity_broken", "肉桂": "continuity_broken"})

    def test_quantity_is_taken_only_when_two_frames_agree(self):
        moving = [("拥有12个", [270, 250, 60, 24]), ("拥有123个", [270, 258, 90, 24]),
                  ("拥有123个", [270, 263, 90, 24]), ("拥有123个", [270, 263, 90, 24])]
        device = Device(Bag(layout("米", *fillers(30))), {}, frames={"米": moving})
        result = run(device, ["米"])
        self.assertEqual(result["quantities"], {"米": 123})

    def test_never_stable_quantity_stays_unknown(self):
        flicker = [("拥有12个", [270, 263, 90, 24]), ("拥有18个", [270, 263, 90, 24])] * 3
        device = Device(Bag(layout("米", *fillers(30))), {}, frames={"米": flicker})
        result = run(device, ["米"], confirm_frames=4)
        self.assertEqual(result["quantities"], {})
        self.assertEqual(result["unknown"], {"米": "detail_unreadable"})

    def test_target_without_template_is_unknown_never_zero(self):
        device = Device(Bag(layout("米", *fillers(30))), {"米": 1})
        result = run(device, ["米", "灵魂鲜奶油"])
        self.assertEqual(result["unknown"], {"灵魂鲜奶油": "template_missing"})
        self.assertFalse(result["complete"])

    def test_stop_keeps_confirmed_reads_and_leaves_the_rest_unknown(self):
        names = ["兽肉", "米", "白糖"]
        device = Device(Bag(layout(*names, *fillers(40))), amounts(*names), stop_after_opens=3)
        result = run(device, [*names, "盐"])
        self.assertEqual(result["termination"], "stopped")
        self.assertEqual(result["quantities"], amounts("兽肉", "米"))
        self.assertEqual(persisted(device), ["兽肉", "米"])
        self.assertEqual(result["unknown"], {"白糖": "stopped", "盐": "stopped"})

    def test_one_recovery_restarts_from_top_then_finishes(self):
        bag = Bag(layout("兽肉", *fillers(40), "胡椒"))
        device = Device(bag, amounts("兽肉", "胡椒"), close_failures=2)
        original, calls = device.list_ready, {"n": 0}

        def flaky(image):
            calls["n"] += 1
            return False if calls["n"] == 2 else original(image)

        device.list_ready = flaky
        result = run(device, ["兽肉", "盐", "胡椒"])
        self.assertTrue(result["recovered"])
        self.assertEqual(device.resets, 1)
        self.assertEqual(result["termination"], "section_end")
        self.assertEqual(result["quantities"], amounts("兽肉", "胡椒"))
        self.assertEqual(result["absent"], ["盐"])

    def test_second_failure_ends_with_state_lost(self):
        device = Device(Bag(layout("兽肉", *fillers(40))), {"兽肉": 1}, list_faults=10)
        result = run(device, ["兽肉", "盐"])
        self.assertEqual(result["termination"], "state_lost")
        self.assertEqual(result["unknown"], {"兽肉": "state_lost", "盐": "state_lost"})

    def test_open_failure_marks_target_unverified(self):
        device = Device(Bag(layout("米", *fillers(40))), {"米": 2}, open_failures={"米": 2})
        result = run(device, ["米", "盐"])
        self.assertEqual(result["unknown"], {"米": "detail_unreadable"})
        self.assertEqual(result["absent"], ["盐"])


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


class CatalogTests(unittest.TestCase):
    """目录项必须有对应资源包自带的模板图：PC 目录只能引用 PC 包里的图，退回 base 的图尺寸不对，会把有的物品扫成 0。"""

    def test_every_catalog_template_exists_in_its_own_pack(self):
        import json
        from utils.name_i18n import canon
        items = json.loads((ROOT / "agent/data/bd2_item_names_i18n.json").read_text(encoding="utf-8"))
        ingredients = {item["cn"] for item in items if item.get("category") == "Ingredients"}
        for pack in ("base", "pc"):
            pipeline = json.loads((ROOT / f"assets/resource/{pack}/pipeline/BagScan.json").read_text(encoding="utf-8"))
            catalog = pipeline["Agt_BagScan_Ingredients_Catalog"]["attach"]["templates"]
            for name, path in catalog.items():
                with self.subTest(pack=pack, name=name):
                    self.assertTrue((ROOT / f"assets/resource/{pack}/image" / path).is_file(), path)
                    self.assertEqual(canon(name), name)
                    self.assertIn(name, ingredients)


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
                "finished_at": "2026-10-28T01:02:00+00:00", "job_id": 7, "profile": "p",
                "target_count": 3, "complete": False, "termination": "section_end", "continuity": "verified",
                "pages": 4, "quantities": {"兽肉": 5}, "absent": ["盐"], "unknown": {"胡椒": "detail_unreadable"},
                "timing_ms": {"total": 1000}, **extra}

    def test_record_goes_to_events_and_latest_summary_by_label(self):
        saved = inventory_store.save_scan_record(self.record())
        self.assertEqual(saved["seq"], 1)
        inventory = MemoryAccountStore.data["arbitrage"]["inventory"]
        event = inventory["events"][-1]
        self.assertEqual((event["kind"], event["label"], event["quantities"]), ("inventory_scan", "地图吸取前", {"兽肉": 5}))
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


if __name__ == "__main__":
    unittest.main()
