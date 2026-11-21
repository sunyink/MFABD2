"""Bounded card selection for teleport return, without NPC or travel actions."""

import hashlib
import math
import time

import numpy as np

from .return_teleport_catalog import CATEGORIES, ReturnCard


class CardUnavailable(RuntimeError):
    def __init__(self, kind, reason):
        if kind not in ("not_visible", "locked", "unsupported"):
            raise ValueError("Unknown card availability result")
        self.kind, self.reason = kind, reason
        super().__init__(reason)


class CardRejectionUnclosed(RuntimeError):
    """A card rejection dialog could not be confirmed closed; stop the batch."""

    def __init__(self, kind, reason):
        if kind not in ("locked", "unsupported"):
            raise ValueError("Unknown unclosed rejection kind")
        self.kind, self.reason = kind, reason
        super().__init__(reason)


class CardTransitionTimeout(RuntimeError):
    """Unfinished loading/transition is fatal to the remaining batch."""


class StoryDialogueBlocked(RuntimeError):
    """Story interaction requires user input and must stop the whole batch."""


class ReturnCardNavigator:
    TIME_LIMIT = 180
    INPUT_LIMIT = 30
    PAGE_LIMIT = 7
    _TABS = {"story": "StoryTab", "character": "CharacterTab", "event": "EventTab"}
    _SELECTED = {"story": "StorySelected", "character": "CharacterSelected", "event": "EventSelected"}

    def __init__(self, context):
        self.context = context
        self.controller = context.tasker.controller
        self.deadline = time.monotonic() + self.TIME_LIMIT
        self.actions = 0
        self._image = None
        self._cache = {}
        self._result_names = {}
        self._overrides = {}
        self._category = None

    def check_running(self):
        if self.context.tasker.stopping:
            raise RuntimeError("返回传送阵切卡已停止")
        if getattr(self.controller, "connected", True) is False:
            raise RuntimeError("返回传送阵切卡控制器已断开")
        if time.monotonic() >= self.deadline:
            raise CardTransitionTimeout("返回传送阵切卡超过180秒；停止后续卡带，避免在加载中连续误跳")

    def _clear_cache(self):
        self._image = None
        self._cache.clear()
        self._result_names.clear()

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check_running()
            time.sleep(min(.1, max(0, end - time.monotonic())))

    def capture(self):
        self.check_running()
        self._clear_cache()
        job = self.controller.post_screencap()
        end = time.monotonic() + 10
        while not job.done:
            self.check_running()
            if time.monotonic() >= end:
                raise RuntimeError("返回传送阵切卡截图超时")
            time.sleep(.1)
        self.check_running()
        if not job.succeeded:
            raise RuntimeError("返回传送阵切卡截图失败")
        image = job.get()
        if image is None or not image.size or image.shape != (720, 1280, 3):
            raise RuntimeError("返回传送阵切卡仅支持1280×720客户区截图")
        return image

    def recognize(self, suffix, image):
        self.check_running()
        if image is not self._image:
            self._clear_cache()
            self._image = image
        if suffix not in self._cache:
            result = self.context.run_recognition("ReturnCards_" + suffix, image, self._overrides)
            if result is None:
                raise RuntimeError("返回传送阵切卡识别无法执行：" + suffix)
            self._cache[suffix] = result
            self._result_names[id(result)] = suffix
        return self._cache[suffix]

    def hit(self, suffix, image):
        return bool(self.recognize(suffix, image).hit)

    def field_ready(self, image):
        return (not self.hit("QuickPanel", image)
                and not self.hit("GuideMenu", image)
                and not self.hit("ConfirmDialog", image)
                and not self.hit("Locked", image)
                and not self.hit("Unsupported", image)
                and self.hit("Field", image))

    def _send(self, suffix, overrides=None):
        self.check_running()
        if self.actions >= self.INPUT_LIMIT:
            raise RuntimeError("返回传送阵切卡达到30次操作上限")
        self.actions += 1
        self._clear_cache()
        result = self.context.run_action("ReturnCards_" + suffix, pipeline_override=overrides or {})
        self.check_running()
        if result is None or not result.success:
            raise RuntimeError("返回传送阵切卡操作失败：" + suffix)

    def click(self, result, offset=(0, 0)):
        # Re-recognize the same target immediately before input; an old result
        # never supplies the click coordinates for a later frame.
        suffix = self._result_names.get(id(result))
        if suffix is None or not result.hit:
            raise CardUnavailable("unsupported", "切卡点击目标缺少当前识别来源")
        image = self.capture()
        self._guard_story_dialogue(image)
        if suffix == "Card" or suffix in self._TABS.values():
            self._failure(image)
        fresh = self.recognize(suffix, image)
        if not fresh.hit or fresh.box is None:
            raise CardUnavailable("unsupported", "切卡前目标已变化：" + suffix)
        if suffix == "Card" and not self._panel_ready(image):
            raise CardUnavailable("unsupported", "切卡前分类面板已变化")
        box = list(fresh.box)
        if (len(box) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)
                or box[2] <= 0 or box[3] <= 0 or box[0] < 0 or box[1] < 0
                or box[0] + box[2] > 1280 or box[1] + box[3] > 720):
            raise RuntimeError("返回传送阵切卡点击范围无效")
        x, y = int(box[0] + box[2] / 2 + offset[0]), int(box[1] + box[3] / 2 + offset[1])
        if not (0 <= x < 1280 and 0 <= y < 720):
            raise RuntimeError("返回传送阵切卡点击超出客户区")
        self._send("Click", {"ReturnCards_Click": {"target": [x, y, 1, 1]}})

    def action(self, suffix, override=None):
        # These are the only fixed primitives used by the shared panel opener
        # and this selector. Each is guarded by a fresh screen observation.
        image = self.capture()
        self._guard_story_dialogue(image)
        if suffix == "OpenQuickPanel":
            if not self.field_ready(image):
                raise CardUnavailable("unsupported", "打开快速卡带前未确认自由场景")
            button = self.recognize("QuickButton", image)
            if not button.hit:
                raise CardUnavailable("unsupported", "未识别到快速卡带按钮；不会固定点地面或走路")
            # Use the observed button, with click() rechecking it on a fresh
            # frame. Never execute the old fixed-coordinate opening primitive.
            self.click(button)
            return
        elif suffix == "MenuBack":
            allowed = self.hit("GachaHeading", image) and not self.hit("GachaPurchaseDialog", image)
        elif suffix in ("TabStart", "TabForward", "ScrollToStart", "ScrollForward"):
            allowed = self.hit("QuickPanel", image)
            if suffix.startswith("Scroll"):
                allowed = allowed and self._panel_ready(image)
        else:
            raise RuntimeError("不允许的切卡操作：" + suffix)
        if not allowed:
            raise CardUnavailable("unsupported", "切卡操作前画面不符：" + suffix)
        self._send(suffix, override)

    def _guard_story_dialogue(self, image):
        if self.hit("StoryDialogue", image):
            raise StoryDialogueBlocked("检测到剧情对话选项；不自动推进剧情，已停止后续卡带")

    def _is_loading(self, image):
        if self.hit("Loading", image):
            return True
        if isinstance(image, np.ndarray) and image.ndim == 3 and image.shape[2] == 3:
            luminance = image.mean(axis=2)
            return float(luminance.mean()) < 25.0 and float((luminance > 120).mean()) < 0.005
        return False

    def _failure(self, image):
        self._guard_story_dialogue(image)
        kind = ("locked" if self.hit("Locked", image) else
                "unsupported" if self.hit("Unsupported", image) else None)
        if kind is None:
            return
        reason = "游戏明确提示卡带尚未解锁" if kind == "locked" else "游戏明确提示当前卡带无法进入"
        # Never confirm a story/unlock/purchase prompt. Only a recognized
        # cancel or back button may dismiss an explicit rejection.
        dismissed = False
        for suffix in ("Cancel", "Back"):
            result = self.recognize(suffix, image)
            if result.hit:
                self.click(result)
                end = time.monotonic() + 3.0
                while time.monotonic() < end:
                    self.pause(.4)
                    fresh = self.capture()
                    self._guard_story_dialogue(fresh)
                    if (not self.hit("Locked", fresh) and not self.hit("Unsupported", fresh)
                            and (self.hit("QuickPanel", fresh) or self.field_ready(fresh))):
                        dismissed = True
                        break
                break
        if not dismissed:
            raise CardRejectionUnclosed(kind, reason + "；拒绝弹窗未能确认关闭")
        raise CardUnavailable(kind, reason)

    def wait_for(self, predicate, description, timeout=20, stable=1, **unused):
        end, consecutive = time.monotonic() + min(timeout, 30), 0
        while time.monotonic() < end:
            image = self.capture()
            self._failure(image)
            consecutive = consecutive + 1 if predicate(image) else 0
            if consecutive >= stable:
                return image
            self.pause(.4)
        self.check_running()
        raise CardUnavailable("unsupported", "未确认" + description)

    def wait_for_card_entry(self, *, require_leave=False):
        # Once a card was clicked, a slow loading screen or an unrecognized
        # opening transition must not be treated as a failed card followed by
        # clicks on every remaining card. Wait without input until the same
        # card has a stable field, an explicit rejection, or the hard deadline.
        left_previous = not require_leave
        leave_deadline = time.monotonic() + 10
        consecutive = 0
        while time.monotonic() < self.deadline:
            image = self.capture()
            if self._is_loading(image):
                left_previous = True
                consecutive = 0
            else:
                self._failure(image)
                if not self.field_ready(image):
                    if not self.hit("QuickPanel", image):
                        left_previous = True
                    consecutive = 0
                elif not left_previous:
                    if time.monotonic() >= leave_deadline:
                        raise CardUnavailable("unsupported", "点击目标卡带后画面未离开原场景，未确认切卡生效")
                    consecutive = 0
                else:
                    consecutive += 1
                    if consecutive >= 2:
                        return image
            self.pause(.6)
        self.check_running()
        raise CardTransitionTimeout("卡带加载或开场转场未结束；停止后续卡带")

    def open_quick_panel(self):
        # Same bounded known-screen recovery as the stealing panel opener;
        # no dependency on its run(), NPC or destination navigation methods.
        image = self.capture()
        if self._is_loading(image):
            image = self.wait_for_card_entry()
        self._failure(image)
        for _ in range(3):
            if self.hit("GachaPurchaseDialog", image):
                self.click(self.recognize("GachaCancel", image))
            elif self.hit("GachaHeading", image):
                self.action("MenuBack")
            else:
                break
            self.pause(1.)
            image = self.capture()
        if self.hit("QuickPanel", image):
            return image
        back = self.recognize("LobbyReturn", image)
        if back.hit:
            self.click(back)
            self.pause(1.)
            image = self.wait_for(
                lambda shot: not self._is_loading(shot) and self.field_ready(shot),
                "从大厅返回自由场景",
                timeout=25,
                stable=2,
            )
        else:
            if not self.field_ready(image):
                raise CardUnavailable("unsupported", "请先关闭未知交互或菜单，回到自由场景或大厅")
            image = self.wait_for(
                lambda shot: not self._is_loading(shot) and self.field_ready(shot),
                "稳定的自由场景",
                timeout=6,
                stable=2,
            )
        button = self.recognize("QuickButton", image)
        if not button.hit:
            raise CardUnavailable("unsupported", "未识别到快速卡带按钮；不会固定点地面或走路")
        self.click(button)
        return self.wait_for(lambda shot: self.hit("QuickPanel", shot), "快速卡带面板", timeout=8)

    def _panel_ready(self, image):
        return self.hit("QuickPanel", image) and self.hit(self._SELECTED[self._category], image)

    def _select_category(self):
        suffix = self._TABS[self._category]
        # Inspect the visible tab strip before trying to reposition it.
        for step in range(5):
            image = self.capture()
            self._failure(image)
            if not self.hit("QuickPanel", image):
                raise CardUnavailable("unsupported", "选择分类时快速卡带面板消失")
            if self._panel_ready(image):
                return image
            tab = self.recognize(suffix, image)
            if tab.hit:
                self.click(tab)
                self.pause(.6)
                return self.wait_for(self._panel_ready, "卡带分类已选中", timeout=5)
            if step < 4:
                self.action("TabStart" if step < 2 else "TabForward")
                self.pause(.6)
        raise CardUnavailable("unsupported", "未找到卡带分类：" + self._category)

    @staticmethod
    def page_signature(image):
        # The artwork strip excludes animated badges and the tab selection.
        # Coarse block means tolerate single-pixel effects without confusing
        # unchanged pages with progress; PAGE_LIMIT is a second hard bound.
        strip = image[604:674, 0:1240, :]
        means = np.asarray(strip, dtype=np.float32).reshape(10, 7, 62, 20, 3).mean(axis=(1, 3))
        return hashlib.blake2b((means / 16).astype(np.uint8).tobytes(), digest_size=16).hexdigest()

    def _try_current_page(self, *, require_leave=False):
        image = self.capture()
        self._failure(image)
        if not self._panel_ready(image):
            raise CardUnavailable("unsupported", "查找卡带时分类面板已变化")
        target = self.recognize("Card", image)
        if not target.hit:
            return image, False
        self.click(target, offset=(-10, 5))
        self.pause(.8)
        self.wait_for_card_entry(require_leave=require_leave)
        return image, True

    def select_card(self, card: ReturnCard, *, require_leave: bool = False) -> None:
        if (not isinstance(card, ReturnCard) or card.category not in CATEGORIES
                or type(card.number) is not int or not 1 <= card.number <= 100
                or not isinstance(card.templates, tuple) or not card.templates
                or any(not isinstance(name, str) or not name
                                            or ":" in name or ".." in name.replace("\\", "/").split("/")
                                            or name.startswith(("/", "\\")) for name in card.templates)):
            raise ValueError("无效的返回传送阵卡带定义")
        self.deadline = time.monotonic() + self.TIME_LIMIT
        self.actions = 0
        self._clear_cache()
        self._category = card.category
        self._overrides = {"ReturnCards_Card": {"template": list(card.templates),
                           "threshold": .80 if (card.category, card.number) == ("story", 1) else .85}}
        self.check_running()
        info = self.controller.info
        if not isinstance(info, dict) or str(info.get("type", "")).lower() != "win32":
            raise RuntimeError("返回传送阵切卡仅支持Windows PC客户端")
        self.open_quick_panel()
        self._select_category()
        _, found = self._try_current_page(require_leave=require_leave)
        if found:
            return
        for _ in range(2):
            self.action("ScrollToStart")
            self.pause(.6)
        seen = set()
        for page in range(self.PAGE_LIMIT):
            image, found = self._try_current_page(require_leave=require_leave)
            if found:
                return
            signature = self.page_signature(image)
            if signature in seen:
                # Re-capture after a bounded settle pause before concluding the
                # scroll reached the end; a slow swipe can still be moving.
                self.pause(.5)
                settled, found = self._try_current_page(require_leave=require_leave)
                if found:
                    return
                signature = self.page_signature(settled)
                if signature in seen:
                    break
            seen.add(signature)
            if page + 1 < self.PAGE_LIMIT:
                self.action("ScrollForward")
                self.pause(.6)
        raise CardUnavailable("not_visible", f"受控查找未发现{card.category}卡{card.number}；不能据此判为未解锁")
