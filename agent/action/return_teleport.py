"""Skill-only teleport return, with optional independent card-scope traversal."""

import json
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from utils import mfaalog


class ReturnTeleportUnavailable(RuntimeError):
    """A card cannot be restored from its current state; other cards may run."""


def parse_options(params, skip_default=True):
    if params is None:
        params = {}
    if not isinstance(params, dict) or set(params) - {"scope", "skip_if_at_portal"}:
        raise ValueError("复原设置仅接受scope和skip_if_at_portal")
    scope = params.get("scope", "current")
    skip = params.get("skip_if_at_portal", skip_default)
    if scope not in ("current", "steal", "all"):
        raise ValueError("复原范围必须是current、steal或all")
    if type(skip) is not bool:
        raise ValueError("已在传送阵跳过设置必须为布尔值")
    return scope, skip


class ReturnTeleportRunner:
    TIME_LIMIT = 150
    INPUT_LIMIT = 16

    def __init__(self, context, skip_if_at_portal=True):
        if type(skip_if_at_portal) is not bool:
            raise ValueError("skip_if_at_portal必须为布尔值")
        self.context = context
        self.controller = context.tasker.controller
        self.deadline = time.monotonic() + self.TIME_LIMIT
        self.inputs = 0
        self._frame = None
        self._cache = {}
        self.skip_if_at_portal = skip_if_at_portal
        self.result = None

    def check_running(self):
        if self.context.tasker.stopping:
            raise RuntimeError("任务已停止")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("返回传送阵超过150秒，已停止；不会改为徒步")

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check_running()
            time.sleep(min(0.2, max(0, end - time.monotonic())))

    def capture(self):
        self.check_running()
        self._frame = None
        self._cache.clear()
        job = self.controller.post_screencap()
        end = time.monotonic() + 10
        while not job.done:
            self.check_running()
            if time.monotonic() >= end:
                raise RuntimeError("返回传送阵截图超时")
            time.sleep(0.1)
        self.check_running()
        if not job.succeeded:
            raise RuntimeError("返回传送阵截图失败")
        frame = job.get()
        if frame is None or not frame.size or frame.shape != (720, 1280, 3):
            raise RuntimeError("返回传送阵仅支持1280×720客户区截图")
        return frame

    def recognize(self, suffix, frame):
        self.check_running()
        if frame is not self._frame:
            self._frame = frame
            self._cache.clear()
        if suffix not in self._cache:
            result = self.context.run_recognition("ReturnTP_" + suffix, frame)
            if result is None:
                raise RuntimeError("无法执行返回传送阵识别：" + suffix)
            self._cache[suffix] = result
        return self._cache[suffix]

    def hit(self, suffix, frame):
        return bool(self.recognize(suffix, frame).hit)

    def click_box(self, box):
        self.check_running()
        if box is None:
            raise RuntimeError("返回传送阵的点击目标不存在")
        values = list(box)
        if len(values) != 4:
            raise RuntimeError("返回传送阵点击范围无效")
        x, y, width, height = values
        if (width <= 0 or height <= 0 or x < 0 or y < 0
                or x + width > 1280 or y + height > 720):
            raise RuntimeError("返回传送阵点击范围超出客户区")
        if self.inputs >= self.INPUT_LIMIT:
            raise RuntimeError("返回传送阵达到16次操作上限")
        self.inputs += 1
        self._frame = None
        self._cache.clear()
        point = [int(x + width / 2), int(y + height / 2), 1, 1]
        result = self.context.run_action("ReturnTP_Click", pipeline_override={
            "ReturnTP_Click": {"target": point}})
        self.check_running()
        if result is None or not result.success:
            raise RuntimeError("返回传送阵点击失败")

    def click(self, suffix, frame):
        result = self.recognize(suffix, frame)
        if not result.hit:
            raise RuntimeError("返回传送阵未识别到目标：" + suffix)
        self.click_box(result.box)

    def field_ready(self, frame):
        return (self.hit("FieldHud", frame)
                and not self.hit("MapMenu", frame)
                and not self.hit("ConfirmDialog", frame)
                and not self.hit("NpcMenu", frame)
                and not self.hit("StoryDialogue", frame)
                and not self.hit("Moving", frame))

    def guard_story_dialogue(self, frame):
        if self.hit("StoryDialogue", frame):
            raise RuntimeError("检测到剧情对话选项；不自动推进剧情，停止复原")

    def near_circle(self, frame):
        return (self.field_ready(frame)
                and self.hit("PortalIdentity", frame)
                and self.hit("PortalInteract", frame))

    def interactable_field(self, frame, *, allow_world_hand=False):
        if not self.field_ready(frame):
            return False
        if self.hit("PortalInteract", frame):
            return True
        return allow_world_hand and self.hit("PortalWorldHand", frame)

    def _click_portal_interact(self, frame, *, allow_world_hand=False):
        if self.hit("PortalInteract", frame):
            self.click("PortalInteract", frame)
            return
        if allow_world_hand and self.hit("PortalWorldHand", frame):
            self.click("PortalWorldHand", frame)
            return
        raise RuntimeError("返回传送阵未识别到目标：PortalInteract")

    def wait_for(self, predicate, description, timeout=15, stable=1, ignore_near_warning=False):
        end = time.monotonic() + min(timeout, 30)
        consecutive = 0
        while time.monotonic() < end:
            frame = self.capture()
            self.guard_story_dialogue(frame)
            if self.hit("NpcMenu", frame):
                self.close_known_menu(frame)
                raise ReturnTeleportUnavailable("交互打开了NPC菜单，已关闭；本卡不继续施法")
            if (self.hit("BlockedToast", frame)
                    and not (ignore_near_warning and self.hit("NearCircleToast", frame))):
                raise ReturnTeleportUnavailable("当前地点无法使用生成魔法阵技能；请先回到允许使用的室外场景，不会徒步")
            if predicate(frame):
                consecutive += 1
                if consecutive >= stable:
                    return frame
            else:
                consecutive = 0
            self.pause(0.4)
        raise ReturnTeleportUnavailable("未确认" + description + "；不会改为徒步")

    def close_known_menu(self, frame):
        for _ in range(4):
            self.guard_story_dialogue(frame)
            if self.hit("ConfirmDialog", frame):
                # A generation prompt can hide the map heading. Cancel its
                # recognized button before attempting to leave the map.
                self.click("Cancel", frame)
            elif self.hit("MapMenu", frame):
                self.click("Back", frame)
            elif self.hit("NpcMenu", frame):
                self.click("Back", frame)
            else:
                return frame
            self.pause(0.6)
            frame = self.capture()
        if self.hit("ConfirmDialog", frame) or self.hit("MapMenu", frame) or self.hit("NpcMenu", frame):
            raise RuntimeError("传送阵菜单未能关闭")
        return frame

    def verify_arrival(self, frame, require_identity=True, allow_world_hand=False):
        ready = (
            self.near_circle
            if require_identity
            else lambda shot: self.interactable_field(shot, allow_world_hand=allow_world_hand)
        )
        if not ready(frame):
            raise RuntimeError("未同时确认传送阵物件/名称和交互按钮")
        self._click_portal_interact(frame, allow_world_hand=allow_world_hand)
        menu = self.wait_for(lambda shot: self.hit("MapMenu", shot), "传送阵交互地图", timeout=8,
                             ignore_near_warning=not require_identity)
        self.close_known_menu(menu)
        # The real interaction-map transition has now proved the object. Its
        # visual rim may change after the camera returns, without any movement.
        self.wait_for(
            lambda shot: self.interactable_field(shot, allow_world_hand=allow_world_hand),
            "传送阵旁的稳定可交互位置",
            timeout=10,
            stable=2,
            ignore_near_warning=not require_identity,
        )
        return True

    def check_existing_portal(self, frame):
        """Prove a real portal interaction before even opening the skill.

        A generic hand is only permission for a reversible interaction probe,
        never proof of completion. When the probe shows the position is not
        on a portal (NPC menu, an inert interaction, or an unknown closable
        menu), the scene is brought back to a verified free field first; the
        skill cast itself then re-validates everything (page, skill ready,
        block toast), so indoor positions still fail with their precise
        reason instead of an interaction-related message.
        """
        if not self.interactable_field(frame):
            return False
        self.click("PortalInteract", frame)
        end = time.monotonic() + 4
        while time.monotonic() < end:
            probe = self.capture()
            self.guard_story_dialogue(probe)
            if self.hit("NpcMenu", probe):
                self.close_known_menu(probe)
                self.wait_for(self.field_ready, "关闭NPC菜单后返回场景", timeout=8, stable=2)
                mfaalog.info("[回到传送阵] 附近是NPC而非传送阵；不再交互，直接尝试技能归位。")
                return False
            if self.hit("MapMenu", probe):
                self.close_known_menu(probe)
                self.wait_for(self.interactable_field, "已在阵上的稳定位置", timeout=10, stable=2,
                              ignore_near_warning=True)
                self.result = "already"
                return True
            self.pause(.3)
        probe = self.capture()
        if self.field_ready(probe):
            mfaalog.info("[回到传送阵] 附近交互未打开传送阵地图；直接尝试技能归位。")
            return False
        if self.hit("Back", probe):
            self.click("Back", probe)
            self.wait_for(self.field_ready, "返回已知自由场景", timeout=10, stable=2)
            mfaalog.info("[回到传送阵] 附近交互不是已验证的传送阵，已返回；尝试技能归位。")
            return False
        raise ReturnTeleportUnavailable("附近交互不是传送阵，且无法安全返回；未使用传送技能")

    def ensure_skill(self, frame):
        if not self.hit("Page3Ready", frame):
            if not self.hit("Page3Button", frame):
                raise RuntimeError("未找到第3技能页，请先关闭其他界面")
            self.click_box([1215, 672, 4, 6])
            self.pause(0.6)
            frame = self.wait_for(lambda shot: self.field_ready(shot) and self.hit("Page3Ready", shot),
                                  "第3技能页", timeout=5)
        if not self.hit("SkillConfigured", frame):
            raise ReturnTeleportUnavailable("第3技能页未配置生成魔法阵；请先配置该技能，本任务不会更换其他技能")
        if not self.hit("SkillReady", frame):
            if self.hit("SkillGray", frame):
                raise ReturnTeleportUnavailable("生成魔法阵技能尚未就绪；不会改为徒步")
            if self.hit("SkillDisabled", frame):
                raise ReturnTeleportUnavailable("当前场景不可使用生成魔法阵技能（如室内）；不会改为徒步")
            raise ReturnTeleportUnavailable("无法确认生成魔法阵技能已就绪")
        if not self.hit("SkillButton", frame):
            raise RuntimeError("未识别到生成魔法阵技能按钮")
        return frame

    def run(self):
        self.check_running()
        info = self.controller.info
        if not isinstance(info, dict) or str(info.get("type", "")).lower() != "win32":
            raise RuntimeError("返回传送阵仅支持Windows PC客户端控制器")
        frame = self.capture()
        self.guard_story_dialogue(frame)
        if self.hit("NpcMenu", frame):
            self.close_known_menu(frame)
            raise ReturnTeleportUnavailable("当前处于NPC菜单，已关闭；本卡未施法")
        frame = self.close_known_menu(frame)
        if self.hit("BlockedToast", frame):
            raise ReturnTeleportUnavailable("当前地点禁止使用生成魔法阵技能；请先回到允许使用的室外场景")
        if not self.field_ready(frame):
            frame = self.wait_for(self.field_ready, "可操作的静止自由场景", timeout=12, stable=2)
        if self.skip_if_at_portal and self.check_existing_portal(frame):
            mfaalog.info("[回到传送阵] 已验证当前位置在阵上，跳过施法。")
            return True
        frame = self.capture()
        frame = self.ensure_skill(frame)
        mfaalog.info("[回到传送阵] 传送技能已就绪，打开技能地图。")
        self.click("SkillButton", frame)
        menu = self.wait_for(
            lambda shot: self.hit("MapMenu", shot)
            or (self.hit("NearCircleToast", shot) and self.interactable_field(shot)),
            "生成魔法阵的地图菜单或已在阵旁的明确提示", timeout=10, stable=2, ignore_near_warning=True)
        if self.hit("NearCircleToast", menu) and self.interactable_field(menu):
            # Only the explicit 'near a magic circle' rejection after casting
            # enables this path. A generic hand icon never bypasses generation.
            result = self.verify_arrival(menu, require_identity=False)
            self.result = "already"
            return result
        portal = self.recognize("MapPortal", menu)
        if not portal.hit:
            raise ReturnTeleportUnavailable("当前显示地图没有可识别的传送阵；不会翻页或切换卡带")
        mfaalog.info("[回到传送阵] 已识别当前地图传送阵，确认生成。")
        self.click_box(portal.box)
        confirm = self.wait_for(lambda shot: self.hit("ConfirmDialog", shot),
                                "生成魔法阵标题和确认按钮", timeout=8, stable=2)
        self.click("Generate", confirm)
        mfaalog.info("[回到传送阵] 已确认生成，等待传送和阵旁交互校验。")
        # Old world-circle templates do not cover every natural blue circle.
        # Generation followed by a real interaction-map transition is the
        # proof of arrival; a hand icon alone is never a successful outcome.
        # Some PC maps (e.g. Story Card 7) intermittently omit the bottom-right F
        # shortcut after teleporting directly onto the portal even though the
        # portal's world-space overhead hand bubble is active and opens MapMenu.
        world_hand_after = time.monotonic() + 10
        arrived = self.wait_for(
            lambda shot: self.interactable_field(
                shot, allow_world_hand=time.monotonic() >= world_hand_after
            ),
            "传送后已在传送阵交互范围",
            timeout=25,
            stable=2,
        )
        used_world_hand = not self.hit("PortalInteract", arrived) and self.hit("PortalWorldHand", arrived)
        if used_world_hand:
            mfaalog.info("[回到传送阵] 右下角交互键未弹出，改用传送阵上方交互气泡打开地图校验。")
        result = self.verify_arrival(arrived, require_identity=False, allow_world_hand=used_world_hand)
        self.result = "restored"
        return result


def restore_cards(context, cards, skip_if_at_portal=True, navigator_factory=None,
                  runner_factory=ReturnTeleportRunner):
    from .return_teleport_cards import CardUnavailable, ReturnCardNavigator
    navigator_factory = navigator_factory or ReturnCardNavigator
    outcomes = []
    started = time.monotonic()
    last_verified = None
    labels = {"story": "剧情", "character": "角色", "event": "活动"}
    for index, card in enumerate(cards, 1):
        if context.tasker.stopping or time.monotonic() - started > 5400:
            mfaalog.error("[批量复原] 任务停止或超过总时限，没有启动后续地图。")
            return False, outcomes
        label = f"{labels[card.category]}卡{card.number}"
        mfaalog.info(f"[批量复原] {index}/{len(cards)} {label}：开始检查。")
        runner = None
        try:
            navigator_factory(context.clone()).select_card(card)
            runner = runner_factory(context.clone(), skip_if_at_portal=skip_if_at_portal)
            if not runner.run():
                raise ReturnTeleportUnavailable("没有确认传送阵归位")
            status = runner.result or "restored"
            outcomes.append({"card": label, "status": status, "reason": ""})
            if status in ("restored", "already"):
                last_verified = (card, label)
            mfaalog.info(f"[批量复原] {label}：{'已在阵上，未施法' if status == 'already' else '技能归位已确认'}。")
        except CardUnavailable as exc:
            outcomes.append({"card": label, "status": exc.kind, "reason": str(exc)})
            mfaalog.error(f"[批量复原] {label}：{exc}，继续下一张。")
        except ReturnTeleportUnavailable as exc:
            outcomes.append({"card": label, "status": "failed", "reason": str(exc)})
            mfaalog.error(f"[批量复原] {label}：{exc}，继续下一张。")
            cleanup = getattr(runner, "close_known_menu", None)
            if cleanup:
                try:
                    cleanup(runner.capture())
                except Exception as cleanup_error:
                    mfaalog.error(f"[批量复原] 无法恢复已知场景：{cleanup_error}；停止后续地图。")
                    return False, outcomes
        except Exception as exc:
            outcomes.append({"card": label, "status": "interrupted", "reason": str(exc)})
            mfaalog.error(f"[批量复原] {label}：{exc}；已停止后续地图。")
            return False, outcomes
    counts = {status: sum(item['status'] == status for item in outcomes)
              for status in ("restored", "already", "locked", "not_visible", "unsupported", "failed")}
    good = counts['restored'] + counts['already']
    failed = counts['not_visible'] + counts['unsupported'] + counts['failed']
    mfaalog.info(f"[批量复原] 目录{len(cards)}张：技能归位{counts['restored']}，已在阵上{counts['already']}，"
                 f"锁定{counts['locked']}，未完成{failed}。")
    if (outcomes and outcomes[-1]["status"] not in ("already", "restored")
            and last_verified is not None):
        if context.tasker.stopping or time.monotonic() - started > 5400:
            mfaalog.error("[批量复原] 已停止或超过总时限，不再执行收尾切卡。")
            return False, outcomes
        card, label = last_verified
        mfaalog.info(f"[批量复原] 最后一张未确认归位，返回本批最后成功的{label}复核；原失败记录保留。")
        try:
            navigator_factory(context.clone()).select_card(card)
            if context.tasker.stopping:
                raise RuntimeError("任务已停止，不再执行收尾复核")
            final_runner = runner_factory(context.clone(), skip_if_at_portal=True)
            if (not final_runner.run() or final_runner.result not in ("already", "restored")
                    or context.tasker.stopping):
                raise RuntimeError("收尾位置未通过独立传送阵验证")
            mfaalog.info(f"[批量复原] 最终停留：{label}，传送阵位置已独立复核；原逐卡结果未改写。")
        except Exception as exc:
            mfaalog.error(f"[批量复原] 收尾未确认：{exc}；未再切换其他地图。")
            return False, outcomes
    elif last_verified is not None:
        mfaalog.info(f"[批量复原] 最终停留：{last_verified[1]}，本轮已确认传送阵位置。")
    return good > 0 and failed == 0, outcomes


@AgentServer.custom_action("ReturnTeleport")
class ReturnTeleportAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            raw = argv.custom_action_param
            params = json.loads(raw) if isinstance(raw, str) else raw
            getter = getattr(context, "get_node_object", None)
            settings = getter("ReturnTP_OptimizeConfig") if getter else None
            attach = getattr(settings, "attach", {}) or {}
            scope, skip = parse_options(params, attach.get("skip_if_at_portal", True))
            if scope != "current":
                from .return_teleport_catalog import load_catalog, select_cards
                result, _ = restore_cards(context, select_cards(load_catalog(), scope), skip)
                return result
            result = ReturnTeleportRunner(context.clone(), skip_if_at_portal=skip).run()
            mfaalog.info("[回到传送阵] 已确认传送阵可交互，菜单已关闭，人物保持原地。")
            return bool(result)
        except Exception as exc:
            mfaalog.error(f"[回到传送阵] {exc}")
            return False
