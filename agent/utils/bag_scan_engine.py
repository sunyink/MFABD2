"""背包仓检的按页扫描逻辑，不依赖框架，设备操作与整页读图由调用方注入。

每页先由整页读图（bag_cells.read_page）给出每个格子是什么，这里只决定：哪些格子要点开、读到的数怎么采信、
什么时候可以判 0、何时结束。注入的设备见 agent/action/bag_scan.py 的 MaaBagDevice；设计见 priv
doc/agent/bag/背包仓检_设计.md §4、§16。

身份以点开读到的名字为准，图标只负责省掉点开（用户 11-11、11-12）：
- 图标认定是「本次不找的」或「已经读过数的」物品 → 不点开；
- 图标认定是还没读数的目标、几个物品共用的图标、彼此相像的券 → 点开读名读数；
- 有东西但图标认不出 → 点开一次；同一趟里再遇到同一张图就不重复点。

判 0 的四个前提缺一不可：这个物品所在的那一段走完了（段尾核实过，或到了列表底）；从第一页起每次翻页都接得上；
它本轮没有「点开了却没核实」；走过的格子个个有着落（认得出，或点开读过名）。读到的数不受这四条约束。
"""

from dataclasses import dataclass, field
import time

import numpy as np

from .bag_cells import same_picture
from .ocr_item_name import clean, name_candidates, resolve_ocr_name
from .ocr_read import NUMBER, unique_parsed


QUANTITY_PATTERN = r"(?:[拥擁]有|持有)" + NUMBER + r"[个個]"
_CONTINUITY_MARGIN = 4.0
_STATIC_ROW = 3.0  # 两帧同一行的平均差低于它算没动；实机静止区约 1.6～2.0
_NAME_SCORE = 0.3      # 名称识别里低于它的碎片（稀有度菱形被读成字，实测约 0.2）不要；偏低的整段交给重读
_FOREIGN_SCORE = 0.8   # 目录外的名字要读得这么清楚才算数
_STRAY_SCORE = 0.5     # 空格之后的格子还能认到这个分数，说明前面那个「空格」可疑


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


@dataclass
class Section:
    """列表里连成一段的一类物品，段走完就能当场给段里没见到的物品判 0，不必扫到列表底。

    head：从列表开头起的一段（料理食材排序下的食材与料理）。run：中间连成一段的（装备制作材料），
    见到过段里的物品之后再走出去才算走完；整段一个都没有时等到列表底。
    """
    name: str
    members: frozenset
    kind: str = "head"
    evidence: str = "section_end"
    entered: bool = False
    strikes: int = 0
    closed: bool = False


@dataclass(frozen=True)
class _Seen:
    """一个格子的着落。icon：凭图标认定；read：点开读到名录里的名字；foreign：点开读到目录外的名字；
    unverified：点开了但没核实。names 是它可能是的物品。"""
    how: str
    names: frozenset = frozenset()


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


def _within(a, b, limit):
    """两个字符串的编辑距离是否不超过 limit（短串，逐行动态规划）。"""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


class BagScanEngine:
    def __init__(self, device, reader, targets, *, known_names, grid_roi, row_height, columns, footer_rows=0,
                 sections=(), loose_kinds=(), loose_names=(), ambiguous=(), aliases=None,
                 max_pages=40, timeout_seconds=900, confirm_frames=4, box_tolerance=3,
                 continuity_threshold=12.0, on_page=None, log=None, clock=time.monotonic):
        """reader：截图 → bag_cells.Page（每格：空 / 认定的图标 / 认不出）。

        known_names：名录里有的目录物品；请求里不在其中的记 template_missing（读到了也对不上号）。
        sections：见 Section。目标不属于任何一段的，扫到列表底才判 0。
        loose_kinds / loose_names：彼此相像、图标信不过的类别（券）与其中的目录物品；这类格子一律点开，由读名定。
        ambiguous：两个不同物品共用的名字（简体「牛奶」既是食材也是活动道具）；读到它时要图标也认定才算数。
        aliases：OCR 名 → 规范名；None 用共用名录。
        """
        self.device = device
        self.reader = reader
        self.grid_roi = list(grid_roi)
        self.row_height = float(row_height)
        self.columns = int(columns)
        self.footer_rows = int(footer_rows)
        self.sections = list(sections)
        self.loose_kinds = frozenset(loose_kinds)
        self.loose_names = frozenset(loose_names)
        self.ambiguous = frozenset(ambiguous)
        self.aliases = aliases
        self.max_pages = int(max_pages)
        self.confirm_frames = max(2, int(confirm_frames))
        self.box_tolerance = int(box_tolerance)
        self.continuity_threshold = float(continuity_threshold)
        self.on_page = on_page or (lambda page, quantities: None)
        self.log = log or (lambda level, text: None)
        self.clock = clock
        self.deadline = clock() + float(timeout_seconds)
        self.order = []
        self.unknown = {}
        for name in dict.fromkeys(targets):
            if name in known_names:
                self.order.append(name)
            else:
                self.unknown[name] = "template_missing"
        self.pending = set(self.order)
        self.quantities = {}
        self.absent = []
        self.absent_by = {}
        self.zero_evidence = None
        self.suspect = set()
        self.blind = 0              # 没有着落的格子数：大于 0 时不许判 0
        self.pages = 0
        self.continuous = True
        self.recovered = False
        self.termination = None
        self.error = None
        self.scroll = 0
        self.origin = None
        self.done = {}              # (绝对行, 列) → _Seen：相邻两页重叠的行不重复处理
        self.remembered = []        # 认不出但点开读过的图：[(比对区像素, _Seen)]
        self.stats = {"opened": 0, "by_icon": 0, "same_picture": 0, "unverified": 0}
        self.timing = {"read": 0.0, "swipe": 0.0, "open": [], "confirm": [], "close": []}
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
            "absent_by": {evidence: sorted(names) for evidence, names in self.absent_by.items()},
            "pages": self.pages,
            "quantities": dict(self.quantities),
            "absent": sorted(self.absent),
            "unknown": dict(sorted(self.unknown.items())),
            "recovered": self.recovered,
            "error": self.error,
            "cells": dict(self.stats),
            "timing_ms": {
                "total": round((self.clock() - started) * 1000),
                "read": round(self.timing["read"] * 1000),
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
                    self._begin_pass()
                self.pages += 1
                started = self.clock()
                page = self.reader(image)
                self.timing["read"] += self.clock() - started
                outcome = self._work_page(page)
                self._flush_page()
                if not self.pending:
                    self.termination = self.termination or "all_found"
                    return
                if outcome != "bottom":
                    if self.pages >= self.max_pages:
                        self._finish("page_limit")
                        return
                    outcome, image = self._next_page(image)
                if outcome == "bottom":
                    self._conclude([name for name in self.order if name in self.pending], "list_bottom")
                    self.termination = "list_bottom"
                    return
            except _Restart:
                self._flush_page()
                image = None

    def _begin_pass(self):
        """从列表顶部（重新）走起：格子编号、各段的进度从头算，上一趟没着落的格子会重新遇到。"""
        self.scroll = 0
        self.origin = None
        self.done = {}
        self.blind = 0
        for section in self.sections:
            if not section.closed:
                section.entered, section.strikes = False, 0

    # ------------------------------------------------------------------ 一页
    def _work_page(self, page):
        """按阅读顺序处理这一页的格子。返回 "bottom" = 这一页已经看到列表的尽头。"""
        cells = page.cells
        if self.origin is None and page.tops:
            self.origin = page.tops[0]
        blank_at = next((index for index, cell in enumerate(cells) if cell.empty), None)
        live = cells if blank_at is None else cells[:blank_at]
        rest = [] if blank_at is None else cells[blank_at:]
        # 列表是连续的，空格之后不该再有物品。空格后面还清楚认得出东西，说明那个「空格」判错了：
        # 这一页不当作到底，那格算没有着落。只是一点杂影（分数很低）则不理它。
        doubtful = any(not cell.empty and (cell.icon is not None or cell.score >= _STRAY_SCORE) for cell in rest)
        if doubtful:
            self.log("warning", f"第 {self.pages} 页空格之后还有认得出的物品，空格判定可疑，本轮不判 0")
            self.blind += 1
            live = [cell for cell in cells if not cell.empty]
        counts = {"icon": 0, "read": 0, "foreign": 0, "unverified": 0}
        for cell in live:
            self._check()
            key = (round((cell.box[1] + self.scroll - (self.origin or 0)) / self.row_height), cell.col)
            if key in self.done:
                continue
            seen = self._settle(cell)
            self.done[key] = seen
            counts[seen.how] += 1
            self._advance(seen)
            if not self.pending:
                break
        self.log("debug", f"第 {self.pages} 页：{len(cells)} 格，空 {sum(cell.empty for cell in cells)}，"
                          f"新处理 {sum(counts.values())}（凭图标 {counts['icon']}、读名 {counts['read']}、"
                          f"目录外 {counts['foreign']}、没核实 {counts['unverified']}）")
        if blank_at is not None and not doubtful and len(rest) >= self.columns:
            return "bottom"
        return None

    def _settle(self, cell) -> _Seen:
        """给一个有物品的格子找着落：能凭图标定的不点开，其余点开读名。"""
        icon = cell.icon
        names, loose = frozenset(), False
        if icon is not None:
            names = frozenset(icon.names)
            loose = bool(icon.kinds & self.loose_kinds)
            if not (names & self.pending or (loose and self.pending & self.loose_names)):
                self.stats["by_icon"] += 1
                return _Seen("icon", names)
        elif cell.patch is not None:
            for patch, seen in self.remembered:
                if same_picture(patch, cell.patch):
                    self.stats["same_picture"] += 1
                    return seen
        # 认不出的格子先只看名字：它多半不是目标（箱子、新出的活动道具），用不着数量；
        # 箱子的详情还多一行，数量不在老位置，等「名称和数量都可读」会一直等不到。
        status, name, quantity, closed = self._inspect(cell.target, peek=icon is None)
        self.stats["opened"] += 1
        seen = self._judge(icon, names, loose, status, name, quantity)
        if icon is None and cell.patch is not None and seen.how in ("read", "foreign"):
            self.remembered.append((cell.patch, seen))
        if not closed:
            self._ensure_list()
        return seen

    def _judge(self, icon, names, loose, status, name, quantity) -> _Seen:
        if status == "named" and name in self.ambiguous and name not in names:
            # 两个不同物品同一个名字：图标认定是另一个，就不是它；图标也认不出，这个名字本轮不许判 0。
            if icon is not None:
                return _Seen("foreign")
            if name in self.pending:
                self.suspect.add(name)
            self.stats["unverified"] += 1
            return _Seen("unverified", frozenset({name}))
        if status == "named":
            if name in self.pending:
                if quantity is not None:
                    self.pending.discard(name)
                    self.quantities[name] = quantity
                    self._page_reads[name] = quantity
                else:
                    self.suspect.add(name)      # 读出了名字却没读成数量：只有这个名字没核实
            return _Seen("read", frozenset({name}))
        if status == "foreign":
            return _Seen("foreign")
        # 没点开，或名字没读出来：这格是谁不知道。
        self.stats["unverified"] += 1
        if icon is not None:
            blocked = set(names & self.pending)
            if loose:
                blocked |= self.pending & self.loose_names
            self.suspect.update(blocked)
        else:
            self.blind += 1
        return _Seen("unverified", names)

    def _advance(self, seen):
        """按新处理的一格推进各段：见到段内物品算进了段；段外物品凭图标要连着两格、点开读过名的一格就算走出。"""
        if seen.how == "unverified":
            return
        for section in self.sections:
            if section.closed:
                continue
            if seen.names & section.members:
                section.entered, section.strikes = True, 0
                continue
            if section.kind == "run" and not section.entered:
                continue
            section.strikes += 1 if seen.how == "icon" else 2
            if section.strikes >= 2:
                section.closed = True
                names = [name for name in self.order if name in self.pending and name in section.members]
                self._conclude(names, section.evidence)
                self.log("info", f"「{section.name}」这一段走完，段内还没见到的 {len(names)} 项就地结清")
                if not self.pending:
                    self.termination = "section_end"

    def _flush_page(self):
        if not self._page_reads:
            return
        reads = dict(self._page_reads)
        self._page_reads.clear()
        try:
            self.on_page(self.pages, reads)
        except Exception:
            # 没写进存档的读数不算读到：改记未知，扫描随之不完整。
            for name in reads:
                self.quantities.pop(name, None)
                self.unknown[name] = "persist_failed"
            raise

    # ------------------------------------------------------------------ 单个格子
    def _inspect(self, box, peek=False):
        """点开 → 读名称与数量 → 关闭。返回 (状态, 名字, 数量或 None, 是否已关闭)。

        peek：只等名称可读就算点开了；读出来是待查物品，再按老办法等数量、两帧复核。

        状态：named 名录里的名字；foreign 目录外的名字；unreadable 名字没读清；unopened 没点开。
        关闭失败不在这里恢复：调用方先记下读数，再确认列表状态。
        """
        started = self.clock()
        opener = self.device.peek_detail if peek else self.device.open_detail
        reading = opener(box)
        if reading is None:
            reading = self._reopen(opener, box, peek)
        if reading is None:
            return "unopened", None, None, False
        self.timing["open"].append(self.clock() - started)
        started = self.clock()
        try:
            status, name, quantity = self._read(reading)
        except (_Stop, _Timeout):
            self.device.close_detail()
            raise
        self.timing["confirm"].append(self.clock() - started)
        started = self.clock()
        closed = self.device.close_detail() or self.device.close_detail()
        self.timing["close"].append(self.clock() - started)
        return status, name, quantity, closed

    def _reopen(self, opener, box, peek):
        """点开没等到可读：先分清是没点开，还是开了但还没读清。

        没点开（列表还在）就再点一次。已经开了就只接着读、不再点：弹窗开着时再点格子，靠右的格子在弹窗外，
        会把弹窗点掉（11-12 模拟器实跑：OCR 慢的机器上 3 秒不够，两成的格子第一次没等到，其中一次因此丢了列表状态）。
        """
        self._check()
        if self.device.list_ready(self.device.capture()):
            return opener(box)
        for _ in range(self.confirm_frames):
            self._check()
            if peek:
                names = self.device.read_names()
                reading = Reading(names=names) if names else None
            else:
                reading = self.device.read_detail()
            if reading is not None:
                return reading
        return None

    def _read(self, first):
        status, name = self._resolve_name(first.names)
        if status == "unreadable":
            # 「可读」那一帧名称可能还在出现动画里（数量先读全了）：弹窗还开着，再看一眼再下结论。
            self._check()
            status, name = self._resolve_name(self.device.read_names())
        if status != "named" or name not in self.pending:
            return status, name, None       # 不是待查物品：知道它是谁就够了，不必读数
        if not first.quantities:            # 只看了名字点开的：补一帧名称数量都可读的
            for _ in range(self.confirm_frames):
                self._check()
                full = self.device.read_detail()
                if full is not None:
                    first = full
                    break
            else:
                return status, name, None
        previous = self._parse(first)
        if previous[0] is None:
            return status, name, self._scaled_quantity()
        texts = {clean(text) for text, _ in first.names}
        for _ in range(self.confirm_frames - 1):
            self._check()
            reading = self.device.read_detail()
            if reading is None:
                return status, name, None
            current = self._parse(reading)
            # 两帧数量相同、数量文字位置不变、名称也对得上，才说明出现动画已走完、不是虚影。
            if (current[0] == previous[0] and self._near(current[1], previous[1])
                    and self._same_name(reading.names, name, texts)):
                return status, name, current[0]
            previous = current
        return status, name, None

    def _resolve_name(self, names):
        def reread():
            values = []
            for _ in range(2):
                self._check()
                values.extend(self.device.read_names())
            return values

        texts = [(self._plain(text), score) for text, score in names if clean(text) and score >= _NAME_SCORE]
        if not texts:
            return "unreadable", None
        if len(texts) > 1:      # 长名字可能被切成几段：拼起来对得上名录就用拼起来的
            joined = "".join(text for text, _ in texts)
            if clean(joined) in self._alias_table():
                texts = [(joined, min(score for _, score in texts))]
        if not any(name_candidates(text, self.aliases)[0] for text, _ in texts):
            return self._foreign(texts)
        resolved = [resolve_ocr_name(text, score, reread, aliases=self.aliases) for text, score in texts]
        found = {item["name"] for item in resolved}
        if any(not item["confirmed"] for item in resolved) or len(found) != 1:
            return "unreadable", None
        return "named", found.pop()

    def _plain(self, text):
        """去掉名字后面跟着的图案：料理名后面的星级、稀有度菱形会被读成一串相同的字符（实测「三角美乃滋饭团 ***」）。

        只在整段对不上名录、而去掉结尾那串相同字符（最多 5 个）后正好是名录里的名字时才去。
        """
        value, table = clean(text), self._alias_table()
        if value in table:
            return text
        tail = len(value) - len(value.rstrip(value[-1]))
        head = value[:-tail]
        return head if 1 <= tail <= 5 and head in table else text

    def _alias_table(self):
        if self.aliases is None:
            from .ocr_item_name import item_aliases
            return item_aliases()
        return self.aliases

    def _foreign(self, texts):
        """名录里连相近的都没有的名字：读得清楚、再读一次还是它，才认作目录外的物品。

        与名录里某个名字只差一两个字、或是它的开头一截的，宁可当没读清——那可能是目录物品读错或被截断，
        当成目录外会把它判成 0。
        """
        text, score = max(texts, key=lambda item: item[1])
        value = clean(text)
        if score < _FOREIGN_SCORE or len(value) < 2:
            return "unreadable", None
        limit = 2 if len(value) >= 5 else 1
        for alias in self._alias_table():
            if len(alias) >= 3 and (alias.startswith(value) or value.startswith(alias)):
                return "unreadable", None
            if len(alias) >= 3 and _within(value, alias, limit):
                return "unreadable", None
        self._check()
        if value not in {clean(again) for again, _ in self.device.read_names()}:
            return "unreadable", None
        return "foreign", value

    def _same_name(self, names, expected, first_texts):
        for text, score in names:
            value = clean(text)
            if value and (value in first_texts
                          or resolve_ocr_name(text, score, aliases=self.aliases)["name"] == expected):
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

    # ------------------------------------------------------------------ 翻页
    def _next_page(self, image):
        outcome = self._swipe(self.device.next_page)
        current, state, shift, detail = self._compare(image)
        if state == "same" and outcome == "bottom":
            # 翻页报到底只说明画面没变：截图失败、滑动没发出去也走同一个出口。
            # 亲自补滑一次并确认动作执行了，列表仍不动才算到底。
            if not self._swipe(self.device.swipe):
                raise RuntimeError("补滑没有执行，不能按滑到底判 0")
            current, state, shift, detail = self._compare(image)
            if state == "same":
                return "bottom", current
            self.log("warning", f"翻页报到底但补滑后列表在动，按正常翻页继续{detail}")
        elif state == "same":
            self.log("warning", f"翻页后列表没动，按列表结束处理{detail}")
            self.continuous = False
            return "bottom", current
        elif outcome == "bottom":
            self.log("warning", f"滑动报到底但画面在动，按正常翻页继续{detail}")
        if state == "broken":
            self.log("warning", f"相邻两页接不上，本轮不再判 0{detail}")
            self.continuous = False
            self.done.clear()       # 位移信不过，格子对不上号：宁可重新处理
        self.scroll += shift or 0
        return "moved", current

    def _swipe(self, action):
        self._check()
        started = self.clock()
        result = action()
        self.timing["swipe"] += self.clock() - started
        return result

    def _compare(self, image):
        """截新一页并与上一页比对，返回 (新截图, 状态, 位移, 日志说明)。"""
        current = self._ready_image()
        shift, state, info = estimate_shift(self._scroll_area(image), self._scroll_area(current),
                                            int(round(self.row_height)), self.continuity_threshold)
        detail = f"（位移 {shift}，{info}）"
        self.log("debug", f"第 {self.pages} 页翻页：{state}{detail}")
        return current, state, shift, detail

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

    def _conclude(self, names, evidence):
        """给一批走完了所在段、仍没见到的物品下结论：四个前提都满足的判 0，否则记未知并写明原因。"""
        self.zero_evidence = evidence
        for name in names:
            if name not in self.pending:
                continue
            if name in self.suspect:
                self.unknown[name] = "detail_unreadable"
            elif not self.continuous:
                self.unknown[name] = "continuity_broken"
            elif self.blind:
                self.unknown[name] = "cell_unverified"
            else:
                self.absent.append(name)
                self.absent_by.setdefault(evidence, []).append(name)
            self.pending.discard(name)

    def _finish(self, termination):
        self.termination = termination
        for name in self.order:
            if name in self.pending:
                self.unknown[name] = termination
        self.pending.clear()
