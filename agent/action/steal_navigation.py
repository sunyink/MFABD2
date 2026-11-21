"""Bounded PC navigation for stealing, without Collect state or talent travel.

The caller supplies a story card, destination and exact target names. The action
only returns success after the requested target is recognized; it never marks an NPC
or collection task complete. All recognition uses the private StealNav nodes.
Clicks and swipes use the caller's native PC controller (PC Cursor recommended),
so the Python agent does not have to steal foreground focus.
"""

import json
import re
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.define import RecognitionDetail
from utils import mfaalog


_PRIVATE_RECOGNITION_NAME = re.compile(r"StealNav_[A-Za-z][A-Za-z0-9_]*\Z")
# Only existing private recognizers with frame-fixed definitions are eligible.
# A future inline alias may share our prefix without being a callable node.
_CACHEABLE_CHILD_SUFFIXES = frozenset({
    "LobbyReturn", "QuickButton", "Field", "QuickPanel", "RecentTab", "StoryTab",
    "Card", "GuideButton", "GuideMenu", "Destination", "ConfirmDialog",
    "ConfirmText", "ConfirmButton", "CancelButton", "MoveUnavailable", "ShopIcon",
    "ShopTarget", "DestinationNpc", "TavernClock", "HostMenuName", "GachaHeading",
    "GachaPurchaseTitle", "GachaCancel", "GachaPurchaseDialog", "AreaName",
    "PreviewPresent", "PreviewCancel", "PreviewDialog", "TalentHeading",
})


def navigation_overrides(params):
    if not isinstance(params, dict) or set(params) - {"area_names"} != {"card", "destination", "expected_npc_names"}:
        raise ValueError("StealNavigate requires card, destination and expected_npc_names")
    card = params["card"]
    destination = params["destination"]
    if type(card) is not int or not 1 <= card <= 8:
        raise ValueError("card must be an integer from 1 through 8")
    if destination not in ("shop", "tavern", "board"):
        raise ValueError("destination must be shop, tavern or board")
    names = params["expected_npc_names"]
    if (not isinstance(names, list) or not 1 <= len(names) <= 4
            or any(not isinstance(name, str) or not 2 <= len(name) <= 40
                   or name != name.strip() or any(character.isspace() for character in name)
                   for name in names)):
        raise ValueError("expected_npc_names must contain 1-4 exact displayed names without whitespace")
    if destination == "board" and not set(names) <= {"探索告示板", "任務布告欄", "探索佈告欄"}:
        raise ValueError("board requires the exact exploration board label as expected_npc_names")
    destination_text = {
        "shop": [r"^商店$"],
        "tavern": [r"酒[馆館]"],
        "board": [r"^探索告示板$", r"^任務布告欄$", r"^探索佈告欄$"],
    }
    overrides = {
        # C1's narrow upstream cover template scored 0.823-0.827 at its actual
        # first-page location in the 2026-10-25 live run. Keep the calibrated
        # tolerance local to C1; all other cards retain the upstream 0.85.
        "StealNav_Card": {"template": [f"Cartridges/Q_sp{card}_PC.png"] +
                          ([f"Cartridges/Steal_sp{card}_active_PC.png"] if card in (2, 3, 4, 5, 6, 7, 8) else []) +
                          (["Cartridges/Steal_sp4_active_art_PC.png"] if card == 4 else []),
                          "threshold": 0.80 if card == 1 else 0.85},
        "StealNav_Destination": {
            "expected": destination_text[destination]
        },
        "StealNav_DestinationNpc": {"expected": ["^" + re.escape(name) + "$" for name in names]},
        "StealNav_HostMenuName": {"expected": ["^" + re.escape(name) + "$" for name in names]},
    }
    # Optional card-identity titles for the same-card fast path. Town prefixes
    # are unique per story card and cover interior variants (guild, shop,
    # tavern, tower floors), mirroring the proven Route MapConfirm titles.
    area_names = params.get("area_names")
    if area_names is not None:
        if (not isinstance(area_names, list) or not 1 <= len(area_names) <= 4
                or any(not isinstance(name, str) or not 2 <= len(name) <= 20
                       or name != name.strip() or any(character.isspace() for character in name)
                       for name in area_names)):
            raise ValueError("area_names must contain 1-4 card title fragments without whitespace")
        overrides["StealNav_AreaName"] = {"expected": list(area_names)}
    return overrides


class StealNavigator:
    """Orchestrate small checked actions; do not borrow Collect next chains."""

    def __init__(self, context, params):
        self.context = context
        self.params = params
        self.deadline = time.monotonic() + 150
        self.actions = 0
        self.confirmed_npc = None
        self._recognition_image = None
        self._recognition_cache = {}

    def check_running(self):
        if self.context.tasker.stopping:
            raise RuntimeError("任务已停止")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("导航超过总时限")

    def pause(self, seconds):
        # Frequent stop checks also keep every individual wait well below 30 s.
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check_running()
            time.sleep(min(0.1, max(0, until - time.monotonic())))

    def capture(self):
        self.check_running()
        self._recognition_image = None
        self._recognition_cache.clear()
        job = self.context.tasker.controller.post_screencap()
        deadline = time.monotonic() + 10
        while not job.done:
            self.check_running()
            if time.monotonic() >= deadline:
                raise RuntimeError("导航截图超过时限")
            time.sleep(0.05)
        if not job.succeeded:
            raise RuntimeError("导航截图失败")
        image = job.get()
        if image is None or not image.size or image.shape[:2] != (720, 1280):
            raise RuntimeError("导航要求1280x720客户区截图")
        return image

    def recognize(self, suffix, image):
        # Several arrival/field guards inspect this exact immutable screenshot
        # repeatedly. Cache only within one frame, never across captures or input.
        if image is not self._recognition_image:
            self._recognition_image = image
            self._recognition_cache.clear()
        if suffix in self._recognition_cache:
            return self._recognition_cache[suffix]
        result = self.context.run_recognition("StealNav_" + suffix, image)
        if result is None:
            raise RuntimeError(f"无法执行导航识别：{suffix}")
        self._recognition_cache[suffix] = result
        self._cache_subresults(result)
        return result

    def _cache_subresults(self, result):
        # Maa already materializes the executed children of And/Or results.
        # Reuse those exact objects, including misses, only within this frame.
        # Short-circuited children have no result and are never inferred here.
        pending = [result]
        visited = set()
        while pending:
            parent = pending.pop()
            if id(parent) in visited:
                continue
            visited.add(id(parent))
            algorithm = getattr(parent, "algorithm", None)
            if getattr(algorithm, "value", algorithm) not in ("And", "Or"):
                continue
            detail = getattr(parent, "best_result", None)
            children = getattr(detail, "sub_results", None)
            if not isinstance(children, (list, tuple)):
                continue
            for child in children:
                # Inline aliases and unrelated recognizers must not become
                # answers for a separately requested private pipeline node.
                if (not isinstance(child, RecognitionDetail)
                        or not isinstance(child.name, str)
                        or not _PRIVATE_RECOGNITION_NAME.fullmatch(child.name)
                        or type(child.hit) is not bool
                        or type(child.reco_id) is not int or child.reco_id <= 0):
                    continue
                suffix = child.name[len("StealNav_"):]
                if suffix not in _CACHEABLE_CHILD_SUFFIXES:
                    continue
                self._recognition_cache.setdefault(suffix, child)
                pending.append(child)

    def hit(self, suffix, image):
        return bool(self.recognize(suffix, image).hit)

    def wait_for(self, predicate, description, timeout=20, stable=1, allow_arrived=False):
        # The cap keeps every wait bounded; loading frames extend the real wait
        # for card entries, so callers may ask for up to 120 s here.
        deadline = time.monotonic() + min(timeout, 120)
        consecutive = 0
        while time.monotonic() < deadline:
            image = self.capture()
            if self.loading(image):
                # Card switches can hold a blank loader for tens of seconds.
                # Loading frames never satisfy predicates but do not fail them.
                consecutive = 0
                self.pause(1.0)
                continue
            if self.hit("MoveUnavailable", image):
                # The game can reject moving to the same place. Only a complete
                # target identity check may turn that rejection into arrival.
                if allow_arrived and self.target_ready(image):
                    return image
                raise RuntimeError("当前地区不允许快速移动")
            if predicate(image):
                consecutive += 1
                if consecutive >= stable:
                    return image
            else:
                consecutive = 0
            self.pause(0.4)
        raise RuntimeError(f"未确认{description}")

    def loading(self, image):
        # The card-loading screen is uniformly dark with no HUD. A dimmed
        # night scene still carries bright HUD icons, so require that bright
        # pixels are almost absent before calling a frame a loader.
        luminance = image.mean(axis=2)
        return float(luminance.mean()) < 25.0 and float((luminance > 120).mean()) < 0.005

    def settle_loading(self, image, timeout=90):
        deadline = time.monotonic() + min(timeout, 100)
        while self.loading(image):
            self.check_running()
            if time.monotonic() >= deadline:
                raise RuntimeError("加载画面等待超时")
            self.pause(1.0)
            image = self.capture()
        return image

    def action(self, suffix, override=None):
        self.check_running()
        self._recognition_image = None
        self._recognition_cache.clear()
        self.actions += 1
        if self.actions > 24:
            raise RuntimeError("导航达到操作次数上限")
        result = self.context.run_action("StealNav_" + suffix, pipeline_override=override or {})
        if result is None or not result.success:
            raise RuntimeError(f"导航操作失败：{suffix}")

    def click(self, result, offset=(0, 0)):
        if not result.hit or result.box is None:
            raise RuntimeError("点击目标未识别")
        x, y, width, height = list(result.box)
        if width <= 0 or height <= 0:
            raise RuntimeError("点击目标没有有效范围")
        x = int(x + width / 2 + offset[0])
        y = int(y + height / 2 + offset[1])
        if not (0 <= x < 1280 and 0 <= y < 720):
            raise RuntimeError("点击目标超出客户区")
        self.action("Click", {"StealNav_Click": {
            "target": [x, y, 1, 1]
        }})

    def field_ready(self, image):
        return (not self.hit("QuickPanel", image)
                and not self.hit("GuideMenu", image)
                and not self.hit("ConfirmDialog", image)
                and self.hit("Field", image))

    def target_ready(self, image):
        destination = self.params["destination"]
        if destination not in ("shop", "board", "tavern"):
            return False
        if not self.field_ready(image):
            return False
        if destination == "shop" and not self.hit("ShopTarget", image):
            return False
        identity = self.recognize("DestinationNpc", image)
        if not identity.hit:
            return False
        results = getattr(identity, "filtered_results", None) or []
        recognized = {
            "".join((item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")).split())
            for item in results
        }
        matches = recognized.intersection(self.params["expected_npc_names"])
        if len(matches) != 1:
            return False
        self.confirmed_npc = matches.pop()
        return True

    def open_quick_panel(self):
        image = self.settle_loading(self.capture())
        # Daily tasks or a failed stealing route can leave the draw page, a
        # stealing preview modal, or an NPC talent menu open. Only these
        # explicitly recognized screens may use this bounded recovery.
        for _ in range(4):
            if self.hit("PreviewDialog", image):
                self.click(self.recognize("PreviewCancel", image))
            elif self.hit("TalentHeading", image) and not self.hit("PreviewPresent", image):
                self.action("CloseHostInteraction")
            elif self.hit("GachaPurchaseDialog", image):
                self.click(self.recognize("GachaCancel", image))
            elif self.hit("GachaHeading", image):
                self.action("MenuBack")
            else:
                break
            self.pause(1.0)
            image = self.capture()
        if self.hit("QuickPanel", image):
            return image
        back = self.recognize("LobbyReturn", image)
        if back.hit:
            self.click(back)
            self.pause(1.0)
            image = self.wait_for(self.field_ready, "从大厅返回战场", timeout=25, stable=2)
        if not self.field_ready(image):
            raise RuntimeError("请先关闭交互或菜单，回到自由场景或大厅")
        button = self.recognize("QuickButton", image)
        if button.hit:
            self.click(button)
        else:
            # Only allowed after a field HUD has been positively recognized.
            self.action("OpenQuickPanel")
        self.pause(0.5)
        return self.wait_for(lambda shot: self.hit("QuickPanel", shot), "快速卡带面板", timeout=8)

    def _dismiss_opened_host_menu(self):
        # Best-effort cleanup when HostInteract opened a wrong or unreadable NPC
        # menu so a failed route does not leave an indoor shopkeeper menu stuck open.
        try:
            probe = self.capture()
            if not self.field_ready(probe) and not self.loading(probe):
                self.try_action("CloseHostInteraction")
                self.pause(1.0)
        except Exception:
            pass

    def reveal_tavern_host(self, image):
        # Only the observed tavern interaction icon authorizes this check.
        # No refresh/recruitment or paid option is selected.
        if (self.params["destination"] != "tavern" or not self.field_ready(image)
                or not self.hit("TavernClock", image)):
            raise RuntimeError("未确认酒馆主人交互图标")
        self.action("HostInteract")
        self.pause(1.0)
        try:
            self.wait_for(lambda shot: not self.field_ready(shot) and self.hit("HostMenuName", shot),
                          "酒馆主人交互姓名", timeout=8)
        except Exception:
            self._dismiss_opened_host_menu()
            raise
        self.action("CloseHostInteraction")
        self.pause(1.0)
        self.wait_for(self.target_ready, "返回已确认的酒馆主人旁", timeout=8, stable=2)

    def reveal_shop_host(self, image):
        # Upgrade speech bubbles can cover the field name at an outdoor shop.
        # The shop icon only permits opening the interaction; its exact menu
        # name still has to match before this can establish the route anchor.
        if (self.params["destination"] != "shop" or not self.field_ready(image)
                or not self.hit("ShopIcon", image)):
            raise RuntimeError("未确认商店交互图标")
        self.action("HostInteract")
        self.pause(1.0)
        try:
            menu = self.wait_for(lambda shot: not self.field_ready(shot)
                                and self.hit("HostMenuName", shot),
                                "店主交互姓名", timeout=8)
            identity = self.recognize("HostMenuName", menu)
            results = getattr(identity, "filtered_results", None) or []
            recognized = {"".join((item.get("text", "") if isinstance(item, dict)
                                   else getattr(item, "text", "")).split()) for item in results}
            matches = recognized.intersection(self.params["expected_npc_names"])
            if len(matches) != 1:
                raise RuntimeError("店主交互姓名不唯一或不符")
        except Exception:
            self._dismiss_opened_host_menu()
            raise
        self.action("CloseHostInteraction")
        self.pause(1.0)
        self.wait_for(lambda shot: self.field_ready(shot) and self.hit("ShopIcon", shot),
                      "返回已确认的店主旁", timeout=8, stable=2)
        self.confirmed_npc = matches.pop()

    def _quick_panel_alive(self, image):
        # Fast guard while scanning pages on QuickPanel: if QuickButton reappeared,
        # the bottom drawer has already auto-hidden; otherwise RecentTab confirms
        # the category strip in ~0.8s on CPU instead of re-running full StoryTab OCR,
        # with QuickPanel kept as fallback if the tab strip was scrolled horizontally.
        if self.hit("QuickButton", image):
            return False
        return self.hit("RecentTab", image) or self.hit("QuickPanel", image)

    def select_card(self, panel):
        reopened = False
        while True:
            if not self.hit("StoryTab", panel):
                raise RuntimeError("快速卡带中没有剧情分类")
            fresh_panel = self.capture()
            if self.hit("QuickButton", fresh_panel):
                if not reopened and self.field_ready(fresh_panel):
                    reopened = True
                    mfaalog.info("[偷窃导航] 点击剧情分类前快速面板自动收起，重新打开一次。")
                    panel = self.open_quick_panel()
                    continue
                raise RuntimeError("查找卡带时快速面板消失")
            fresh_tab = self.recognize("StoryTab", fresh_panel)
            if not fresh_tab.hit:
                if not reopened and self.field_ready(fresh_panel):
                    reopened = True
                    mfaalog.info("[偷窃导航] 点击剧情分类前未识别分类标签，重新打开快速面板一次。")
                    panel = self.open_quick_panel()
                    continue
                raise RuntimeError("快速卡带中没有剧情分类")
            self.click(fresh_tab)
            self.pause(0.8)
            # No fixed slot: favourites and recent cards can move the desired cover.
            # Search the current page, reset to the beginning, then scan at most 4 pages.
            # Check Card (fast TemplateMatch ~50ms) BEFORE OCR guards so CPU-OCR never
            # stalls long enough for QuickPanel to auto-hide between capture and click.
            panel_reopened = False
            for attempt in range(7):
                image = self.capture()
                if not self.hit("QuickButton", image):
                    card = self.recognize("Card", image)
                    if card.hit:
                        self.click(card, offset=(-10, 5))
                        self.pause(2.0)
                        return self.wait_for(self.field_ready, f"进入剧情卡{self.params['card']}",
                                             timeout=90, stable=2)
                if not self._quick_panel_alive(image):
                    if not reopened and self.field_ready(image):
                        reopened = True
                        panel_reopened = True
                        mfaalog.info("[偷窃导航] 查找卡带时快速面板自动收起，重新打开一次。")
                        panel = self.open_quick_panel()
                        break
                    raise RuntimeError("查找卡带时快速面板消失")
                if attempt == 6:
                    break
                self.action("ScrollToStart" if attempt < 2 else "ScrollForward")
                self.pause(0.8)
            if not panel_reopened:
                break
        raise RuntimeError(f"受控查找后仍未识别剧情卡{self.params['card']}封面")

    def open_guide(self, image):
        for _ in range(2):
            if self.hit("GuideMenu", image):
                return image
            guide = self.recognize("GuideButton", image)
            if not guide.hit:
                raise RuntimeError("未识别场景寻路列表入口")
            self.click(guide)
            self.pause(0.8)
            image = self.capture()
        if not self.hit("GuideMenu", image):
            raise RuntimeError("寻路列表未打开")
        return image

    def try_action(self, suffix):
        # Probe actions must degrade to "not verified" instead of failing the
        # route; the full panel flow below recovers any unknown screen.
        self.check_running()
        result = self.context.run_action("StealNav_" + suffix)
        if result is None or not result.success:
            return False
        self.actions += 1
        return True

    def probe_on_card(self):
        # Cheap card-identity probe reusing the proven route map check: open
        # the area map, require this card's unique town/title fragment, then
        # always close the map again. Titles live on the map panel only, so a
        # plain field frame can never confirm the card by itself.
        if not self.try_action("AreaMapOpen"):
            return False
        verified = False
        closed = False
        try:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                self.check_running()
                if self.hit("AreaName", self.capture()):
                    verified = True
                    break
                self.pause(0.3)
        finally:
            if not self.context.tasker.stopping:
                try:
                    closed = self.try_action("AreaMapClose")
                except Exception:
                    closed = False
        if not closed:
            return False
        self.pause(1.0)
        return verified

    def same_card_field(self, initial):
        # Optional same-card fast path: when the caller already stands in a
        # free field on the requested story card, skip the cartridge panel and
        # the map re-entry entirely. Any miss returns None and the caller runs
        # the unchanged panel flow, so a wrong card can never be assumed.
        if self.params.get("area_names") is None or not self.field_ready(initial):
            return None
        if not self.probe_on_card():
            return None
        return self.capture()

    def settle_at_field(self, field):
        """Resolve arrival, host reveal, or guided travel from a field frame."""
        if self.target_ready(field):
            return True
        if self.params["destination"] == "shop" and self.hit("ShopIcon", field):
            self.reveal_shop_host(field)
            return True
        if self.params["destination"] == "tavern" and self.hit("TavernClock", field):
            self.reveal_tavern_host(field)
            return True
        guide = self.open_guide(field)
        self.enter_destination(guide)
        return True

    def enter_destination(self, guide):
        destination = self.recognize("Destination", guide)
        if not destination.hit:
            # GuideMenu can match while the list is still sliding in, before the
            # lower entries render. Settle once and re-read before failing.
            self.pause(0.6)
            guide = self.capture()
            if not self.hit("GuideMenu", guide):
                raise RuntimeError("寻路列表未稳定或已经改变")
            destination = self.recognize("Destination", guide)
        if not destination.hit:
            raise RuntimeError("寻路列表没有指定目的地")
        self.click(destination)
        self.pause(0.6)
        image = self.wait_for(
            lambda shot: self.hit("ConfirmDialog", shot) or self.target_ready(shot),
            "立即前往确认框", timeout=8, allow_arrived=True)
        if self.target_ready(image):
            return
        # The modal moves while opening. Re-read its identity and button after
        # another settle period; never send the old static confirm coordinate.
        self.pause(0.6)
        image = self.capture()
        if not self.hit("ConfirmDialog", image):
            raise RuntimeError("确认框未稳定或已经改变")
        self.click(self.recognize("ConfirmButton", image))
        self.pause(1.0)
        if self.params["destination"] == "tavern":
            image = self.wait_for(lambda shot: self.target_ready(shot) or
                (self.field_ready(shot) and self.hit("TavernClock", shot)),
                "酒馆主人落点", timeout=30, stable=2, allow_arrived=True)
            if not self.target_ready(image):
                self.reveal_tavern_host(image)
        elif self.params["destination"] == "shop":
            image = self.wait_for(lambda shot: self.target_ready(shot) or
                (self.field_ready(shot) and self.hit("ShopIcon", shot)),
                "商店交互落点", timeout=30, stable=2, allow_arrived=True)
            if not self.target_ready(image):
                self.reveal_shop_host(image)
        else:
            self.wait_for(self.target_ready, "指定目标及交互提示姓名", timeout=30, stable=3, allow_arrived=True)

    def run(self):
        initial = self.capture()
        # Shop callers use town-specific/unique NPC names. Boards (and future
        # taverns) reuse labels across towns, so their identity only becomes
        # meaningful after selecting the requested story card.
        if self.params["destination"] == "shop" and self.target_ready(initial):
            return True
        field = self.same_card_field(initial)
        if field is not None:
            # The card-identity probe replaces the cartridge re-entry, so the
            # guided move starts from the caller's exact position. The game
            # rejects near guide travel ("现在无法移动的区域") and leaves a clean
            # field; rerun the proven panel flow from a fresh card entry then.
            try:
                return self.settle_at_field(field)
            except RuntimeError as exc:
                if "不允许快速移动" not in str(exc):
                    raise
                self.check_running()
                mfaalog.info("[偷窃导航] 同卡快速寻路被游戏拒绝，回退快速卡带流程。")
        panel = self.open_quick_panel()
        field = self.select_card(panel)
        return self.settle_at_field(field)


@AgentServer.custom_action("StealNavigate")
class StealNavigateAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            raw = argv.custom_action_param
            params = json.loads(raw) if isinstance(raw, str) else raw
            overrides = navigation_overrides(params)
            local = context.clone()
            if not local.override_pipeline(overrides):
                raise RuntimeError("无法配置独立导航节点")
            navigator = StealNavigator(local, params)
            result = navigator.run()
            if not navigator.confirmed_npc:
                raise RuntimeError("未核对目标交互提示姓名")
            mfaalog.info(f"[偷窃导航] 卡{params['card']} {params['destination']}：已确认目标 {navigator.confirmed_npc}。")
            return bool(result)
        except Exception as exc:
            mfaalog.error(f"[偷窃导航] {exc}；本次导航失败，不写入完成记录。")
            return False
