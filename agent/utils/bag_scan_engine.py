"""背包仓检的按页扫描逻辑，不依赖框架，设备操作与整页读图由调用方注入。

每页先由整页读图（bag_cells.read_page）给出每个格子是什么，这里只决定：哪些格子要点开、读到的数怎么采信、
什么时候可以判 0、何时结束。注入的设备见 agent/action/bag_scan.py 的 MaaBagDevice；设计见 priv
doc/agent/bag/背包仓检_设计.md §4、§16。

身份以点开读到的名字为准，图标只负责省掉点开（用户 11-11、11-12）：
- 图标认定是「本次不找的」或「已经读过数的」物品 → 不点开；
- 图标认定是还没读数的目标、几个物品共用的图标、彼此相像的券 → 点开读名读数；
- 有东西但图标认不出 → 点开一次；同一趟里再遇到同一张图就不重复点。

格子坐标来自这一页的截图，列表一挪就不准（2026-11-15 PC：点开没反应时列表被带着滚了，后面按旧坐标点到下一行）。
点开没反应、或点开读到的名字与格子图标对不上时，**重新截图分格**，量出挪了多少，同一页接着做（设计 §21）。
图标认得出的格子，图文都相符才采信（用户 11-15）：对不上的那一格不收读数，重新分格后再点；重新分格过还对不上，
就是图标和读名有一个错了，两边的物品本轮都不判 0。

判 0 的四个前提缺一不可：这个物品所在的那一段走完了（段尾核实过，或到了列表底）；从第一页起每次翻页都接得上；
它本轮没有「点开了却没核实」；走过的格子个个有着落（认得出，或点开读过名）。读到的数不受这四条约束。

角标读数（用户 11-16，设计 §23）：图标认定、今天前一次读过数（baseline）的目标，先读格子右下角的数量，
和前一次一样才不点开；不一样、读不出、格式不规范都照旧点开。同一物品可能占几格、而且不一定挨着（11-11 模拟器：
兽肉 192 与两格 99,999 中间隔着别的），所以角标收下的数先算「待定」：同一趟里再见到它的另一格就点开读详情里的总数；
它所在那一段走完、或到了列表底才算定下来；扫描中途结束的，待定的一律记未知、不写进存档。
"""

from dataclasses import dataclass, field
import time

import numpy as np

from .bag_cells import match_picture, same_picture
from .ocr_item_name import clean, name_candidates, resolve_ocr_name
from .ocr_read import NUMBER, unique_parsed


QUANTITY_PATTERN = r"(?:[拥擁]有|持有)" + NUMBER + r"[个個]"
# 正常走完的结束原因。其余（stopped、timeout、state_lost、error、page_limit）是半路断了：
# 没走到的目标记成未知，原因就是这个结束原因；角标收下还没定的记 corner_unconfirmed。
FINISHED = frozenset({"all_found", "list_bottom", "section_end"})
_CONTINUITY_MARGIN = 4.0
_STATIC_ROW = 3.0  # 两帧同一行的平均差低于它算没动；实机静止区约 1.6～2.0
_NAME_SCORE = 0.3      # 名称识别里低于它的碎片（稀有度菱形被读成字，实测约 0.2）不要；偏低的整段交给重读
_FOREIGN_SCORE = 0.8   # 目录外的名字要读得这么清楚才算数
_STRAY_SCORE = 0.5     # 空格之后的格子还能认到这个分数，说明前面那个「空格」可疑
_RESPLITS_PER_PAGE = 2  # 一页最多重新分格几次；用完了按老办法（原坐标补点、信读名）往下走，免得来回打转
_STILL_TRIES = 5        # 重新分格前等列表停稳：最多截这么多张，相邻两张不动就用


class _Stop(Exception):
    pass


class _Resplit(Exception):
    """这一页的格子坐标信不过了（点开没反应、或读到的名字与图标对不上）：重新截图分格，同一页接着做。"""


class _Timeout(Exception):
    pass


class _StateLost(Exception):
    pass


class _Restart(Exception):
    pass


@dataclass
class Reading:
    """一帧详情弹窗上的两段 OCR：名称 [(text, score)]，数量 [(text, score, box)]。

    settled：这一帧由设备核实过「弹窗淡入已走完、画面静止」（MaaBagDevice 截帧判定），读数不必再截帧复核；
    否则（退回原识别节点读的）照旧要相邻两帧一致才采信。
    """
    names: list = field(default_factory=list)
    quantities: list = field(default_factory=list)
    settled: bool = False


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


def _kinds(page):
    """一页里按阅读顺序认出的物品（到第一个空格为止）。认不出、或图标不止对应一种物品的格子记 None；
    整页对齐没把握时图标都信不过，返回 None。"""
    if not getattr(page, "aligned", True):
        return None
    kinds = []
    for cell in page.cells:
        if cell.empty:
            break
        names = cell.icon.names if cell.icon is not None else ()
        kinds.append(names[0] if len(names) == 1 else None)
    return kinds


def joining_kinds(kinds) -> list:
    """上一页末尾的两种物品（按阅读顺序从最后一格往回数，认不出的格子跳过），最后一种在前。"""
    return [kind for kind in dict.fromkeys(reversed(kinds or ())) if kind is not None][:2]


@dataclass(frozen=True)
class _Seen:
    """一个格子的着落。icon：凭图标认定；read：点开读到名录里的名字；foreign：点开读到目录外的名字；
    corner：图标认定、角标读数与前一次一样（待定）；unverified：点开了但没核实。names 是它可能是的物品。"""
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

    一行高的条带碰上同款物品连成的几行会有两个一样好的位置，这时加高到两行再比一次（见函数内注释）。
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
    scores = _scores(a, b, band)
    shift, best, second = _pick(scores, band)
    info.update(best=round(best, 2), second=round(second, 2))
    if best <= threshold and second - best < _CONTINUITY_MARGIN:
        widened, info["widened"] = _widen(a, b, band, scores, threshold)
        if widened is not None:
            shift, best, second = widened
    if best > threshold or second - best < _CONTINUITY_MARGIN:
        return shift, "broken", info
    return shift, "same" if shift <= 2 else "moved", info


def _scores(a, b, band):
    """上一页底部高 band 的条带放在新页每个位移处的平均差。"""
    height = a.shape[0]
    strip = a[height - band:]
    return np.array([np.mean(np.abs(b[height - band - s:height - s] - strip)) for s in range(height - band + 1)])


def _pick(scores, row):
    """(最佳位移, 最佳得分, 次佳得分)：次佳取离最佳超过半行的地方，差不到半行的算同一处。"""
    shift = int(np.argmin(scores))
    others = np.concatenate([scores[:max(0, shift - row // 2)], scores[shift + row // 2 + 1:]])
    return shift, float(scores[shift]), float(others.min()) if others.size else float("inf")


def _widen(a, b, band, narrow, threshold):
    """一行高的条带分不开时加高再比。条带落在同款物品连成的几行上（11-13 PC 实机：一整行 99,999 的同一种矿，
    新页里连着两行一样）会有两个差一行、一样好的位置；往上加高，带上上面那行不一样的内容就能分开。

    位移错一行，后面每页的格子都会对错号（可能把没见过的格子当成处理过、误判 0），所以只认画面证据、不猜拖动距离：
    ① 加高后能搜的位移变少，条带只加到所有近优位置都还搜得到为止（最多两行），否则真位置可能根本没被比过；
    ② 加高后最佳要分得开，且落在近优位置上；③ 在那个位置，加高的条带要和一行高时对得一样好（多出来的那截也对上）。
    返回 ((位移, 最佳, 次佳) 或 None, 日志说明)。"""
    height = a.shape[0]
    near = np.flatnonzero(narrow <= narrow.min() + _CONTINUITY_MARGIN)
    tall = min(band * 2, height - int(near.max()))
    if tall < band + band // 4:
        return None, {"tall": tall}
    shift, best, second = _pick(_scores(a, b, tall), band)
    note = {"tall": tall, "shift": shift, "best": round(best, 2), "second": round(second, 2)}
    if (best <= threshold and second - best >= _CONTINUITY_MARGIN and int(np.abs(near - shift).min()) <= 2
            and best - float(narrow[shift]) <= _CONTINUITY_MARGIN / 2):
        return (shift, best, second), note
    return None, note


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
                 continuity_threshold=12.0, on_page=None, log=None, clock=time.monotonic, known_pictures=(),
                 corner=None, baseline=None, full_stacks=(99, 9_999, 99_999)):
        """reader：截图 → bag_cells.Page（每格：空 / 认定的图标 / 认不出）。

        known_names：名录里有的目录物品；请求里不在其中的记 template_missing（读到了也对不上号）。
        sections：见 Section。目标不属于任何一段的，扫到列表底才判 0。
        loose_kinds / loose_names：彼此相像、图标信不过的类别（券）与其中的目录物品；这类格子一律点开，由读名定。
        ambiguous：两个不同物品共用的名字（简体「牛奶」既是食材也是活动道具）；读到它时要图标也认定才算数。
            可以按客户端语言给 {"cn": …, "tw": …}：语言由读到的数量字样定（「持有…個」繁体、「拥有…个」简体），
            还不知道语言时按两种合起来算。
        aliases：OCR 名 → 规范名；None 用共用名录。
        known_pictures：以前的盘点记住的目录外物品的图（utils/bag_pictures.load 的结果）：认不出图标的格子比中了就跳过，不点开。
            这一趟用到了哪几张、新记了哪些，跑完由 pictures() 交给调用方存。
        corner：(这一页的截图, 格子) → 角标读数或 None；None 表示这一趟不读角标、全部点开。
        baseline：{物品: 今天前一次读到的数}，只有它里面的物品才读角标。
        full_stacks：一格放满时的数（物品不同上限不同，11-11 截图见过 99、9,999、99,999）。读到它说明可能还有别的格，
            照旧点开；前一次的数比最大的还大，一定不止一格，不读角标。
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
        if isinstance(ambiguous, dict):
            self.ambiguous_by = {language: frozenset(names) for language, names in ambiguous.items()}
            self.ambiguous = frozenset().union(*self.ambiguous_by.values())
        else:
            self.ambiguous_by, self.ambiguous = {}, frozenset(ambiguous)
        self.language = None        # 客户端语言：读到第一个数量时定下
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
        self.known_pictures = list(known_pictures)
        self._known_stack = (np.stack([entry["patch"] for entry in self.known_pictures]).astype(np.float32)
                             if self.known_pictures and len({entry["patch"].shape for entry in self.known_pictures}) == 1
                             else None)
        self.pictures_seen = set()  # 这一趟比中过的记住的图（下标）
        self.pictures_new = []      # 这一趟点开读出是目录外的图：[(比对区像素, 名字)]
        self.corner = corner
        self.baseline = dict(baseline or {})
        self.full_stacks = frozenset(int(value) for value in full_stacks)
        self.provisional = {}       # 角标收下、还没定下来的：{物品: 数}
        self.by_corner = set()      # 角标读数定下来的物品
        self._corner_recheck = set()  # 这一页从头重走之后，原来那一格会再遇到一次：再遇到先重读角标，一样就是同一格
        self.stats = {"opened": 0, "by_icon": 0, "same_picture": 0, "unverified": 0, "unaligned_pages": 0,
                      "joined_by_item": 0, "resplit": 0, "known_picture": 0,
                      "corner": 0, "corner_missed": 0, "corner_reopened": 0}
        self._resplits_left = 0     # 这一页还能重新分格几次
        self._mismatched = set()    # 重新分格过的「图标 → 读到的名字」：再遇到就是图标认错了，不再重分
        self._page_image = None     # 这一页分格用的截图与结果：重新分格时量挪了多少、查有没有没处理就滚出去的格子
        self._page = None
        self._gap_shift = None      # 刚重新分格、量出的挪动距离（判不出为 None）：读新页时查有没有格子滚出去
        self.tail = None
        self.unjoined = None
        self.timing = {"read": 0.0, "swipe": 0.0, "open": [], "confirm": [], "close": [], "corner": []}
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
            "corner": sorted(self.by_corner),
            "timing_ms": {
                "total": round((self.clock() - started) * 1000),
                "read": round(self.timing["read"] * 1000),
                "swipe": round(self.timing["swipe"] * 1000),
                "detail": {"count": len(self.timing["open"]),
                           "open_median": median(self.timing["open"]),
                           "confirm_median": median(self.timing["confirm"]),
                           "close_median": median(self.timing["close"])},
                "corner": {"count": len(self.timing["corner"]), "median": median(self.timing["corner"])},
            },
        }

    def _scan(self):
        image, resplit = None, None
        while True:
            try:
                if image is None:
                    image, resplit = self._ready_image(), None
                    self._begin_pass()
                if resplit is not None:
                    image = self._resplit(image, resplit)
                    resplit = None
                else:
                    self.pages += 1
                    self._resplits_left = _RESPLITS_PER_PAGE
                started = self.clock()
                page = self.reader(image)
                self.timing["read"] += self.clock() - started
                self._check_gap(page)
                self._page_image, self._page = image, page
                self._settle_turn(page)
                outcome = self._work_page(page)
                self._flush_page()
                if not self.pending and not self.provisional:
                    self.termination = self.termination or "all_found"
                    return
                if outcome != "bottom":
                    if self.pages >= self.max_pages:
                        self._finish("page_limit")
                        return
                    outcome, image = self._next_page(image)
                if outcome == "bottom":
                    self._confirm_corner()
                    self._conclude([name for name in self.order if name in self.pending], "list_bottom")
                    self.termination = "list_bottom"
                    return
            except _Resplit as why:
                self._flush_page()
                self._resplits_left -= 1
                self.stats["resplit"] += 1
                resplit = str(why)
            except _Restart:
                self._flush_page()
                image = None

    def pictures(self):
        """(这一趟比中过的记住的图的下标, 新记的目录外的图 [(像素, 名字)])，交给 utils/bag_pictures.merge 存回去。"""
        return set(self.pictures_seen), list(self.pictures_new)

    def _resplit(self, image, why):
        """同一页重新截图分格：等列表停稳，量出比上次分格时挪了多少，计进滚动量，处理过的格子照旧认得出来。

        量不出（往回挪了、或画面有歧义）时整页从头重走（读过的凭图标跳过，不多点），这一轮不判 0：
        往上挪了多少不知道，就不能保证没有格子没处理就滚出去了。
        """
        current = self._still_image()
        row = int(round(self.row_height))
        shift, state, info = estimate_shift(self._scroll_area(image), self._scroll_area(current), row,
                                            self.continuity_threshold)
        if state == "broken" and info.get("viewport", row * 2) < row * 2:
            # 只有不到两行高的一条在变（按下格子的高亮、提示条）：列表本身没动——列表真挪了，几乎每一行都会变。
            # 翻页那边要量出滚了多少，变化太少只能判不出；这里只问动没动（11-15 PC 实跑：只有最底下 86 行不同，被当成判不出，整轮不判 0）。
            shift, state = 0, "same"
        self._gap_shift = None
        if state == "broken":
            self.done.clear()
            self.blind += 1
            # 处理过的格子不再认得出：角标收下的那一格会再遇到一次，别把它当成同一物品的别的格去点开（11-16 PC 实机白点了 33 格）
            self._corner_recheck = set(self.provisional)
            self.log("warning", f"第 {self.pages} 页{why}，重新截图分格；列表挪了多少判不出（{info}），"
                                "这一页从头重走，本轮不判 0")
        else:
            self.scroll += shift or 0
            self._gap_shift = shift or 0
            moved = "列表没动" if state == "same" else f"列表上移 {shift} 像素"
            self.log("warning", f"第 {self.pages} 页{why}，重新截图分格（{moved}），这一页接着做")
        return current

    def _still_image(self):
        """截到列表停稳的一帧：相邻两帧的列表区不动才用；一直在动就用最后一帧（随后还有图文不符的兜底）。"""
        current = self._list_image()
        for _ in range(_STILL_TRIES - 1):
            following = self._list_image()
            _, state, _ = estimate_shift(self._scroll_area(current), self._scroll_area(following),
                                         int(round(self.row_height)), self.continuity_threshold)
            current = following
            if state == "same":
                break
        return current

    def _list_image(self):
        """重新分格用的一帧列表。弹窗开着多半是点开慢了半拍——判成没反应之后才弹出来（11-15 模拟器实跑）：
        关掉再截，不当成状态异常；关了还不在列表，才走整轮一次的恢复。"""
        self._check()
        image = self.device.capture()
        if self.device.list_ready(image):
            return image
        self.device.close_detail()
        return self._ready_image()

    def _check_gap(self, page):
        """重新分格后：上一张截图里还没处理的格子，挪动后跑到了新页第一行之上（滚出去了），就再也点不到——本轮不判 0。"""
        shift, self._gap_shift = self._gap_shift, None
        if shift is None or self._page is None or not page.tops:
            return
        first = min(page.tops) - self.box_tolerance
        lost = [cell for cell in self._page.cells if not cell.empty and cell.box[1] - shift < first
                and (round((cell.box[1] + self.scroll - shift - (self.origin or 0)) / self.row_height), cell.col)
                not in self.done]
        if lost:
            self.blind += 1
            self.log("warning", f"第 {self.pages} 页列表挪动时有 {len(lost)} 格没处理就滚出去了，本轮不判 0")

    def _settle_turn(self, page):
        """上一次翻页在画面上接不上时，改按物品判：这一页第一格的物品是上一页末尾两种之一，就没有漏掉别的东西。

        依据是同一种物品的格子在列表里连成一片：这一页从上一页末尾见过的物品起头，中间夹着的只可能还是它。
        上一页取末尾两种、对上哪一种都行（用户 2026-11-15）：只取最后一种的话，那一格万一认错就接不上了。
        只证明没漏东西，不管滚了几行：格子的记号在翻页时已经清掉，这一页逐格重新处理，用不着行号。
        """
        previous, self.tail = self.tail, _kinds(page)
        if self.unjoined is None:
            return
        detail, self.unjoined = self.unjoined, None
        head = self.tail
        last = joining_kinds(previous)
        if head and head[0] is not None and head[0] in last:
            self.stats["joined_by_item"] += 1
            self.log("info", f"第 {self.pages - 1} 页翻页在画面上分不清位置，按物品接上了（上一页末尾是{'、'.join(reversed(last))}，"
                             f"这一页从{head[0]}起），照常判 0")
            return
        self.log("warning", f"相邻两页接不上，本轮不再判 0{detail}")
        self.continuous = False

    def _begin_pass(self):
        """从列表顶部（重新）走起：格子编号、各段的进度从头算，上一趟没着落的格子会重新遇到。"""
        self.scroll = 0
        self.origin = None
        self.done = {}
        self.blind = 0
        self.tail = None        # 上一页按阅读顺序认出的物品
        self.unjoined = None    # 上一次翻页画面上接不上时的说明，等读完这一页再定
        # 角标收下还没定的：从头走起会再见到它们的格子，放回待查，到时照样先读角标。
        self.pending.update(self.provisional)
        self.provisional.clear()
        self._corner_recheck.clear()
        for section in self.sections:
            if not section.closed:
                section.entered, section.strikes = False, 0

    # ------------------------------------------------------------------ 一页
    def _work_page(self, page):
        """按阅读顺序处理这一页的格子。返回 "bottom" = 这一页已经看到列表的尽头。"""
        cells = page.cells
        if self.origin is None and page.tops:
            self.origin = page.tops[0]
        if not page.aligned and any(cell.icon is not None for cell in cells):
            # 整页对齐没把握时，凑巧对上的图标信不过：这一页的格子一律当认不出，点开读名（同图的仍只点一次）。
            # 身份全来自读到的名字，所以这一轮照样可以判 0。
            self.stats["unaligned_pages"] += 1
            self.log("warning", f"第 {self.pages} 页整页对齐没有把握，这一页不凭图标跳过，逐格点开读名")
            for cell in cells:
                if cell.icon is not None:
                    cell.icon = None
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
        counts = {"icon": 0, "read": 0, "foreign": 0, "unverified": 0, "corner": 0}
        for cell in live:
            self._check()
            key = (round((cell.box[1] + self.scroll - (self.origin or 0)) / self.row_height), cell.col)
            if key in self.done:
                continue
            seen = self._settle(cell)
            self.done[key] = seen
            counts[seen.how] += 1
            self._advance(seen)
            if not self.pending and not self.provisional:
                break
        self.log("debug", f"第 {self.pages} 页：{len(cells)} 格，空 {sum(cell.empty for cell in cells)}，"
                          f"新处理 {sum(counts.values())}（凭图标 {counts['icon']}、读名 {counts['read']}、"
                          f"角标 {counts['corner']}、目录外 {counts['foreign']}、没核实 {counts['unverified']}）")
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
            again = sorted(names & self.provisional.keys())
            if again and len(names) == 1 and again[0] in self._corner_recheck:
                name = again[0]
                self._corner_recheck.discard(name)
                if self._read_corner(cell, name) == self.provisional[name]:
                    return _Seen("corner", names)          # 重走时又遇到原来那一格
            if again:
                # 角标收下的物品又见到一格：不止一格，角标只是其中一格的数。点开读详情里的总数。
                for name in again:
                    self.provisional.pop(name)
                    self.pending.add(name)
                self.stats["corner_reopened"] += 1
                self.log("info", f"角标读过的{'、'.join(again)}又见到一格，点开读总数")
            elif not (names & self.pending or (loose and self.pending & self.loose_names)):
                self.stats["by_icon"] += 1
                return _Seen("icon", names)
            else:
                seen = self._try_corner(cell, names, loose)
                if seen is not None:
                    return seen
        elif cell.patch is not None:
            for patch, seen in self.remembered:
                if same_picture(patch, cell.patch):
                    self.stats["same_picture"] += 1
                    return seen
            index = match_picture(self._known_stack, cell.patch)
            if index is not None:
                # 以前的盘点点开读过、是目录外的东西（活动道具等）：不再点开（用户 11-15）
                self.stats["known_picture"] += 1
                self.pictures_seen.add(index)
                return _Seen("foreign")
        # 认不出的格子先只看名字：它多半不是目标（箱子、新出的活动道具），用不着数量；
        # 箱子的详情还多一行，数量不在老位置，等「名称和数量都可读」会一直等不到。
        status, name, quantity, closed = self._inspect(cell.target, peek=icon is None)
        self.stats["opened"] += 1
        seen = self._judge(icon, names, loose, status, name, quantity)
        if icon is None and cell.patch is not None and seen.how in ("read", "foreign"):
            self.remembered.append((cell.patch, seen))
            # 只记目录外的：读到的名字不在名录里，或是名录里带 # 的目录外物品。名录里的物品可能是要读数的目标，不记。
            if seen.how == "foreign" or (seen.names and all(str(item).startswith("#") for item in seen.names)):
                self.pictures_new.append((cell.patch, str(name or "")))
        if not closed:
            self._ensure_list()
        if seen.how == "moved":
            shown = "、".join(sorted(names)) or "?"
            raise _Resplit(f"点开读到的是{name or '目录外的东西'}，格子图标是{shown}")
        return seen

    def _try_corner(self, cell, names, loose):
        """图标只认一种物品、不是券、不同名、今天前一次读过数的待查目标：读角标，与前一次一样就先收下（待定），不点开。"""
        if self.corner is None or loose or len(names) != 1:
            return None
        name = next(iter(names))
        expected = self.baseline.get(name)
        if (name not in self.pending or expected is None or name in self.ambiguous
                or expected in self.full_stacks or expected > max(self.full_stacks, default=0)):
            return None
        value = self._read_corner(cell, name)
        if value is None or value != expected:
            self.stats["corner_missed"] += 1
            return None
        self.pending.discard(name)
        self.provisional[name] = value
        self.stats["corner"] += 1
        return _Seen("corner", frozenset({name}))

    def _read_corner(self, cell, name):
        started = self.clock()
        try:
            value = self.corner(self._page_image, cell)
        except Exception as exc:  # 角标只是省点开的捷径：读不了就照旧点开
            self.log("debug", f"角标读数出错（{name}）：{exc}")
            value = None
        self.timing["corner"].append(self.clock() - started)
        return value

    def _confirm_corner(self, members=None):
        """角标收下的数定下来：它所在那一段走完了，或到了列表底——同一趟里没再见到它的别的格子。"""
        for name in [name for name in self.provisional if members is None or name in members]:
            value = self.provisional.pop(name)
            self.quantities[name] = value
            self._page_reads[name] = value
            self.by_corner.add(name)

    def _is_ambiguous(self, name) -> bool:
        table = self.ambiguous_by.get(self.language) if self.language else None
        return name in (self.ambiguous if table is None else table)

    def _note_language(self, reading):
        """从数量字样认客户端语言（繁体「持有…個」、简体「拥有…个」），认出一次就不再改。"""
        if self.language is not None or reading is None:
            return
        texts = "".join(str(text) for text, *_ in reading.quantities)
        if "持有" in texts or "擁有" in texts or "個" in texts:
            self.language = "tw"
        elif "拥有" in texts or "个" in texts:
            self.language = "cn"

    def _judge(self, icon, names, loose, status, name, quantity) -> _Seen:
        if icon is not None and not loose and status in ("named", "foreign"):
            # 图文都相符才采信（用户 11-15）。简体里同名的活动道具「牛奶」认出了自己的图标、读到「牛奶」，也算相符——它是目录外那个。
            twin = status == "named" and self._is_ambiguous(name) and ("#" + name) in names
            if name not in names and not twin:
                if self._resplits_left > 0 and (names, name) not in self._mismatched:
                    # 多半是列表挪了、点到了别的格子：这一格不算数、读数不收、不推进分段，重新分格后再点
                    # （上次正是这样一格被当成「走出存货段」，把 100 项判了 0）。
                    self._mismatched.add((names, name))
                    return _Seen("moved", frozenset({name}) if name else frozenset())
                # 重新分格过还对不上：图标和读名必有一个错了，分不清是哪个——都不采信，两边的物品本轮都不判 0。
                self.suspect.update(names & self.pending)
                if status == "named" and name in self.pending:
                    self.suspect.add(name)
                self.stats["unverified"] += 1
                return _Seen("unverified", names)
            if twin:
                return _Seen("foreign")
        if status == "named" and self._is_ambiguous(name) and name not in names:
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
            section.strikes += 1 if seen.how in ("icon", "corner") else 2
            if section.strikes >= 2:
                section.closed = True
                self._confirm_corner(section.members)
                names = [name for name in self.order if name in self.pending and name in section.members]
                self._conclude(names, section.evidence)
                self.log("info", f"「{section.name}」这一段走完，段内还没见到的 {len(names)} 项就地结清")
                if not self.pending and not self.provisional:
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
            if self._resplits_left > 0:
                # 点击没成立时列表可能被带着挪了（11-15 PC：上移一行，补点打开的是正下方那格）：重新分格再点。
                raise _Resplit("点开没反应")
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
        self._note_language(first)
        status, name = self._resolve_name(first.names)
        if status == "unreadable":
            # 「可读」那一帧名称可能还在出现动画里（数量先读全了）：弹窗还开着，再看一眼再下结论。
            self._check()
            status, name = self._resolve_name(self.device.read_names())
        if status != "named" or name not in self.pending:
            return status, name, None       # 不是待查物品：知道它是谁就够了，不必读数
        texts = {clean(text) for text, _ in first.names}
        if not first.quantities:            # 只看了名字点开的：补一帧名称数量都可读的
            for _ in range(self.confirm_frames):
                self._check()
                full = self.device.read_detail()
                if full is not None:
                    first = full
                    self._note_language(full)
                    break
            else:
                return status, name, None
        previous = self._parse(first)
        if previous[0] is None:
            return status, name, self._scaled_quantity()
        if first.settled and self._same_name(first.names, name, texts):
            return status, name, previous[0]    # 取自已静止的那一帧：再截一帧也是同一张图，不必复核
        texts |= {clean(text) for text, _ in first.names}
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
            # 位移信不过，格子对不上号：宁可重新处理。算不算漏了东西，等读完新的一页按物品再定（_settle_turn）。
            self.unjoined = detail
            self.done.clear()
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
        # 角标收下还没定的：没走完它所在那一段，不知道它是不是还有别的格子，记未知、不写进存档。
        for name in self.provisional:
            self.unknown[name] = "corner_unconfirmed"
        self.provisional.clear()
