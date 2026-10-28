"""背包仓检的按页扫描逻辑，不依赖框架，设备操作由调用方注入。

只管四件事：这一页要找哪些目标、点开读到的数怎么采信、什么时候可以判 0、何时结束。
注入的设备见 agent/action/bag_scan.py 的 MaaBagDevice；设计见 priv
doc/agent/bag/背包仓检_设计.md §4。

判 0 的三个前提缺一不可：到了列表结束处（末端核过名，或确认滑到底）；从第一页起
每次翻页都接得上；该目标本轮没有「模板命中了却没核实」。读到的数不受这三条约束。
"""

from dataclasses import dataclass, field
import time

import numpy as np

from .ocr_item_name import clean, name_candidates, resolve_ocr_name
from .ocr_read import NUMBER, unique_parsed


QUANTITY_PATTERN = r"(?:[拥擁]有|持有)" + NUMBER + r"[个個]"
_CONTINUITY_MARGIN = 4.0
_STATIC_ROW = 3.0  # 两帧同一行的平均差低于它算没动；实机静止区约 1.6～2.0
_END_NAME_SCORE = 0.8
_END_CHECKS = 3


class _Stop(Exception):
    pass


class _Timeout(Exception):
    pass


class _StateLost(Exception):
    pass


class _Restart(Exception):
    pass


@dataclass
class Reading:
    """一帧详情弹窗上的两段 OCR：名称 [(text, score)]，数量 [(text, score, box)]。"""
    names: list = field(default_factory=list)
    quantities: list = field(default_factory=list)


def estimate_shift(previous, current, band, threshold):
    """找出上一页滚动区底部一条带在新截图里的竖直位置，返回 (滚动距离, 状态, 说明)。

    状态：same = 没动；moved = 动了且新页至少还看得见这一条带；broken = 找不到或有歧义。
    带高取一行：找得到就说明滚动距离不超过「可见高度减一行」，任何一行至少在一页里完整出现过。
    比整块二维差值而不比行均值：格子边框按行周期重复，只比行均值会在整行倍数上出假峰。

    列表区上下边缘可能压着不随列表滚动的界面（10-28 实机：模拟器列表区最下 64 行是固定底栏），
    这些行在两帧里完全相同。先把贴着上下边缘的这类行剔掉，只在真正滚动的区域里找；否则底栏会
    让「没动」在差值上占便宜，真动了反而对不上。误剔掉的只会让可视高度变小、判定更严，不会放宽。
    """
    a = np.asarray(previous, dtype=np.float32)
    b = np.asarray(current, dtype=np.float32)
    if a.ndim == 3:
        a = a.mean(axis=2)
    if b.ndim == 3:
        b = b.mean(axis=2)
    if a.shape != b.shape or band <= 0:
        return None, "broken", {}
    full = a.shape[0]
    moving = np.flatnonzero(np.abs(a - b).mean(axis=1) >= _STATIC_ROW)
    if moving.size == 0:
        return 0, "same", {"viewport": full}
    top, bottom = int(moving[0]), int(moving[-1]) + 1
    a, b = a[top:bottom], b[top:bottom]
    height = a.shape[0]
    info = {"viewport": height, "trimmed": [top, full - bottom]}
    if height < band * 2:
        return None, "broken", info
    strip = a[height - band:]
    scores = np.array([np.mean(np.abs(b[height - band - s:height - s] - strip))
                       for s in range(height - band + 1)])
    shift = int(np.argmin(scores))
    best = float(scores[shift])
    others = np.concatenate([scores[:max(0, shift - band // 2)], scores[shift + band // 2 + 1:]])
    second = float(others.min()) if others.size else float("inf")
    info.update(best=round(best, 2), second=round(second, 2))
    if best > threshold or second - best < _CONTINUITY_MARGIN:
        return shift, "broken", info
    return shift, "same" if shift <= 2 else "moved", info


class BagScanEngine:
    def __init__(self, device, catalog, targets, *, grid_roi, row_height, ingredient_names, footer_rows=0,
                 max_pages=30, timeout_seconds=900, confirm_frames=4, box_tolerance=3,
                 continuity_threshold=12.0, zero_evidence="ingredient_boundary",
                 on_page=None, log=None, clock=time.monotonic):
        self.device = device
        self.catalog = dict(catalog)
        self.grid_roi = list(grid_roi)
        self.row_height = int(row_height)
        self.footer_rows = int(footer_rows)
        self.ingredients = set(ingredient_names)
        self.max_pages = int(max_pages)
        self.confirm_frames = max(2, int(confirm_frames))
        self.box_tolerance = int(box_tolerance)
        self.continuity_threshold = float(continuity_threshold)
        self.end_evidence = zero_evidence
        self.zero_evidence = None
        self.on_page = on_page or (lambda page, quantities: None)
        self.log = log or (lambda level, text: None)
        self.clock = clock
        self.deadline = clock() + float(timeout_seconds)
        self.order = []
        self.unknown = {}
        for name in dict.fromkeys(targets):
            if name in self.catalog:
                self.order.append(name)
            else:
                self.unknown[name] = "template_missing"
        self.pending = set(self.order)
        self.quantities = {}
        self.absent = []
        self.suspect = set()
        self.pages = 0
        self.continuous = True
        self.recovered = False
        self.termination = None
        self.error = None
        self.timing = {"match": 0.0, "swipe": 0.0, "open": [], "confirm": [], "close": []}
        self._page_reads = {}

    # ------------------------------------------------------------------ 主循环
    def run(self) -> dict:
        started = self.clock()
        try:
            if self.pending:
                self._scan()
            else:
                self.termination = "all_found"
        except _Stop:
            self._finish("stopped")
        except _Timeout:
            self._finish("timeout")
        except _StateLost:
            self._finish("state_lost")
        except Exception as exc:  # 设备层异常也要交回已读到的部分
            self.error = str(exc)
            self._finish("error")
        try:
            self._flush_page()
        except Exception as exc:
            self.error = self.error or f"逐页写入失败: {exc}"

        def median(values):
            return round(float(np.median(values)) * 1000) if values else None

        return {
            "complete": not self.unknown,
            "termination": self.termination,
            "continuity": "verified" if self.continuous else "broken",
            "zero_evidence": self.zero_evidence,
            "pages": self.pages,
            "quantities": dict(self.quantities),
            "absent": sorted(self.absent),
            "unknown": dict(sorted(self.unknown.items())),
            "recovered": self.recovered,
            "error": self.error,
            "timing_ms": {
                "total": round((self.clock() - started) * 1000),
                "match": round(self.timing["match"] * 1000),
                "swipe": round(self.timing["swipe"] * 1000),
                "detail": {"count": len(self.timing["open"]),
                           "open_median": median(self.timing["open"]),
                           "confirm_median": median(self.timing["confirm"]),
                           "close_median": median(self.timing["close"])},
            },
        }

    def _scan(self):
        image = None
        while True:
            try:
                if image is None:
                    image = self._ready_image()
                self.pages += 1
                self._scan_page(image)
                self._flush_page()
                if not self.pending:
                    self.termination = "all_found"
                    return
                if self._end_confirmed(image):
                    self._conclude(self.end_evidence, "section_end")
                    return
                if self.pages >= self.max_pages:
                    self._finish("page_limit")
                    return
                outcome, image = self._next_page(image)
                if outcome == "bottom":
                    self._conclude("list_bottom", "list_bottom")
                    return
            except _Restart:
                self._flush_page()
                image = None

    # ------------------------------------------------------------------ 一页
    def _scan_page(self, image):
        cells = []
        started = self.clock()
        for name in self.order:
            if name not in self.pending:
                continue
            self._check()
            for box, score in self.device.match(self.catalog[name], image):
                cell = next((cell for cell in cells if self._same_cell(cell["box"], box)), None)
                if cell is None:
                    cells.append({"box": box, "score": score, "names": {name}})
                else:
                    cell["names"].add(name)
                    if score > cell["score"]:
                        cell.update(box=box, score=score)
        self.timing["match"] += self.clock() - started
        cells.sort(key=lambda cell: (round(cell["box"][1] / self.row_height), cell["box"][0]))
        for cell in cells:
            if not cell["names"] & self.pending:
                continue
            name, quantity, closed = self._inspect(cell["box"])
            if name in self.pending and quantity is not None:
                self.pending.discard(name)
                self.quantities[name] = quantity
                self._page_reads[name] = quantity
            # 模板命中的格子读出来是别的物品、或没核实：这些目标本轮不许判 0。
            self.suspect.update(target for target in cell["names"] if target in self.pending)
            if not closed:
                self._ensure_list()

    def _same_cell(self, a, b):
        ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
        bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
        return abs(ax - bx) < self.row_height / 2 and abs(ay - by) < self.row_height / 2

    def _flush_page(self):
        if self._page_reads:
            reads = dict(self._page_reads)
            self._page_reads.clear()
            self.on_page(self.pages, reads)

    # ------------------------------------------------------------------ 单个格子
    def _inspect(self, box):
        """点开 → 读名称与数量 → 关闭。返回 (规范名或 None, 数量或 None, 是否已关闭)。

        关闭失败不在这里恢复：调用方先记下读数，再确认列表状态。
        """
        started = self.clock()
        reading = self.device.open_detail(box) or self.device.open_detail(box)
        if reading is None:
            return None, None, False
        self.timing["open"].append(self.clock() - started)
        started = self.clock()
        try:
            name, quantity = self._read(reading)
        except (_Stop, _Timeout):
            self.device.close_detail()
            raise
        self.timing["confirm"].append(self.clock() - started)
        started = self.clock()
        closed = self.device.close_detail() or self.device.close_detail()
        self.timing["close"].append(self.clock() - started)
        return name, quantity, closed

    def _read(self, first):
        name = self._resolve_name(first.names)
        if name is None:
            return None, None
        previous = self._parse(first)
        if previous[0] is None:
            return name, self._scaled_quantity()
        texts = {clean(text) for text, _ in first.names}
        for _ in range(self.confirm_frames - 1):
            self._check()
            reading = self.device.read_detail()
            if reading is None:
                return name, None
            current = self._parse(reading)
            # 两帧数量相同、数量文字位置不变、名称也对得上，才说明出现动画已走完、不是虚影。
            if (current[0] == previous[0] and self._near(current[1], previous[1])
                    and self._same_name(reading.names, name, texts)):
                return name, current[0]
            previous = current
        return name, None

    def _resolve_name(self, names):
        def reread():
            values = []
            for _ in range(2):
                self._check()
                values.extend(self.device.read_names())
            return values

        texts = [(text, score) for text, score in names if clean(text)]
        if not texts:
            return None
        resolved = [resolve_ocr_name(text, score, reread) for text, score in texts]
        found = {item["name"] for item in resolved}
        if any(not item["confirmed"] for item in resolved) or len(found) != 1:
            return None
        return found.pop()

    @staticmethod
    def _same_name(names, expected, first_texts):
        for text, score in names:
            value = clean(text)
            if value and (value in first_texts or resolve_ocr_name(text, score)["name"] == expected):
                return True
        return False

    @staticmethod
    def _parse(reading):
        values = {}
        for text, _score, box in reading.quantities:
            parsed = unique_parsed([text], QUANTITY_PATTERN)
            if parsed is not None:
                values[parsed[0]] = box
        if len(values) != 1:
            return None, None
        return next(iter(values.items()))

    def _near(self, a, b):
        return (a is not None and b is not None
                and all(abs(int(x) - int(y)) <= self.box_tolerance for x, y in zip(a[:2], b[:2])))

    def _scaled_quantity(self):
        """数量没能完整解析时放大重读；放大图上没有可比的位置，改为连续两次读数一致。"""
        values = []
        for _ in range(2):
            self._check()
            parsed = unique_parsed(self.device.read_quantity_scaled(), QUANTITY_PATTERN)
            values.append(parsed[0] if parsed else None)
        return values[0] if values[0] is not None and values[0] == values[1] else None

    # ------------------------------------------------------------------ 末端与翻页
    def _end_confirmed(self, image):
        """末端标志命中后按位置依次点开核名：名字清晰读出、且不是任何一种食材，才算末端。

        一页最多核 _END_CHECKS 个；误命中在食材上的跳过，接着核下一个。
        """
        boxes = sorted(self.device.end_markers(image),
                       key=lambda item: (round(item[1] / self.row_height), item[0]))
        for box in boxes[:_END_CHECKS]:
            self._check()
            reading = self.device.open_detail(box) or self.device.open_detail(box)
            if reading is None:
                self._ensure_list()
                continue
            if not (self.device.close_detail() or self.device.close_detail()):
                self._ensure_list()
            texts = sorted(((text, score) for text, score in reading.names if clean(text)),
                           key=lambda item: item[1], reverse=True)
            if not texts or texts[0][1] < _END_NAME_SCORE or len(clean(texts[0][0])) < 2:
                self.log("warning", "末端标志处的名称没读清，不算末端")
                continue
            candidates, _ = name_candidates(texts[0][0])
            if candidates & self.ingredients:
                self.log("warning", f"末端标志落在食材「{texts[0][0]}」上，不算末端")
                continue
            return True
        return False

    def _next_page(self, image):
        self._check()
        started = self.clock()
        outcome = self.device.next_page()
        self.timing["swipe"] += self.clock() - started
        current = self._ready_image()
        shift, state, info = estimate_shift(self._scroll_area(image), self._scroll_area(current),
                                            self.row_height, self.continuity_threshold)
        detail = f"（位移 {shift}，{info}）"
        self.log("debug", f"第 {self.pages} 页翻页：{state}{detail}")
        if state == "same":
            if outcome != "bottom":
                self.log("warning", f"翻页后列表没动，按列表结束处理{detail}")
                self.continuous = False
            return "bottom", current
        if outcome == "bottom":
            self.log("warning", f"滑动报到底但画面在动，按正常翻页继续{detail}")
        if state == "broken":
            self.log("warning", f"相邻两页接不上，本轮不再判 0{detail}")
            self.continuous = False
        return "moved", current

    def _scroll_area(self, image):
        """列表区去掉最下方的固定底栏：底栏内容可能变（提示、按钮状态），只拿真正随列表滚动的部分比。"""
        x, y, w, h = self.grid_roi
        return np.asarray(image)[y:y + h - self.footer_rows, x:x + w]

    # ------------------------------------------------------------------ 状态
    def _ready_image(self):
        self._check()
        image = self.device.capture()
        if self.device.list_ready(image):
            return image
        self._recover()
        raise _Restart()

    def _ensure_list(self):
        """打开或关闭等不到信号后，确认还在列表；不在则恢复一次并从头扫。"""
        self._check()
        if self.device.list_ready(self.device.capture()):
            return
        self._recover()
        raise _Restart()

    def _recover(self):
        if self.recovered:
            raise _StateLost()
        self.recovered = True
        self.log("warning", "背包列表状态异常，补一次关闭后回到顶部重扫")
        self.device.close_detail()
        if not self.device.reset_to_top():
            raise _StateLost()
        # 回到顶部重新走一遍，之前的连续性不再作数，判 0 要重新走到结束处。
        self.continuous = True

    def _check(self):
        if self.device.stopping():
            raise _Stop()
        if self.clock() >= self.deadline:
            raise _Timeout()

    def _conclude(self, evidence, termination):
        self.termination = termination
        self.zero_evidence = evidence
        for name in self.order:
            if name not in self.pending:
                continue
            if name in self.suspect:
                self.unknown[name] = "detail_unreadable"
            elif not self.continuous:
                self.unknown[name] = "continuity_broken"
            else:
                self.absent.append(name)
        self.pending.clear()

    def _finish(self, termination):
        self.termination = termination
        for name in self.order:
            if name in self.pending:
                self.unknown[name] = termination
        self.pending.clear()
