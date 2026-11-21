"""Run the three-character stealing UI after the caller has reached an NPC.

The subtask has no persistent completion writes. Its success means all three
visible skill cards carry a recognized weekly countdown for this exact NPC.
"""

import json
import re
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from utils import mfaalog


COOLDOWN_HHMM_PATTERN = (
    r"^(?:(?:[1-9]\d{0,2}|0[1-9]\d?|00[1-9])[:：][0-5]\d|0{1,3}[:：](?:[1-5]\d|0[1-9]))$"
)


def interaction_overrides(params):
    if not isinstance(params, dict) or set(params) != {"npc_names"}:
        raise ValueError("StealInteraction requires only npc_names")
    names = params["npc_names"]
    if (not isinstance(names, list) or not 1 <= len(names) <= 4
            or any(not isinstance(name, str) or not 2 <= len(name) <= 40
                   or name != name.strip() for name in names)):
        raise ValueError("npc_names must contain 1-4 exact displayed NPC names")
    # Exact strings rather than user-provided regular expressions prevent broad
    # identity matching from authorizing a different NPC's interaction.
    escaped = [re.escape(name) for name in names]
    return {
        "StealInteraction_NpcIdentity": {"expected": ["^" + name + "$" for name in escaped]},
        "StealInteraction_PreviewIdentity": {
            "expected": ["^" + name + "持有物列表$" for name in escaped]
        },
    }


def cooldown_conditions(x):
    # Preserve the established HH:MM crop and also accept exact 1-7 day text in
    # direct recognition so MaaFw batch-OCR detection cannot clip a thin leading
    # '1' on '1天'. Reject 0:00 / 00:00 so a ready/zero timer never counts as
    # an active weekly cooldown. Keep the separate detected crop as fallback
    # when card lines add an extra glyph in direct recognition (e.g. '5天一').
    return [
        {"recognition": "OCR", "roi": [x + 8, 593, 62, 23],
         "expected": [COOLDOWN_HHMM_PATTERN, r"^[1-7]\s*天$"], "only_rec": True, "threshold": 0.7},
        {"recognition": "OCR", "roi": [x + 8, 590, 62, 30],
         "expected": [r"^[1-7]\s*天$"], "only_rec": False, "threshold": 0.7},
    ]


def label_conditions(x):
    # Keep the established detector crop first. Read the compact label directly
    # only when countdowns or surrounding text prevent its detection.
    # Accept '葡窃' for 1080p downsampled PaddleOCR font-stroke misreads of '偷窃'.
    return [
        {"recognition": "OCR", "roi": [x + 8, 610, 65, 30],
         "expected": [r"^[偷葡][窃竊]$"]},
        {"recognition": "OCR", "roi": [x + 20, 611, 40, 16],
         "expected": [r"^[偷葡][窃竊]$"], "only_rec": True},
    ]


def card_layout_overrides(offset):
    if type(offset) is not int or offset not in (0, 1):
        raise ValueError("Unrecognized stealing-card layout")
    slots = (88, 170, 252, 334)
    centers = (126, 209, 291, 373)
    overrides = {}
    for character in (1, 2, 3):
        slot = character - 1 + offset
        x = slots[slot]
        overrides[f"StealInteraction_Card{character}Cooldown"] = {"any_of": cooldown_conditions(x)}
        overrides[f"StealInteraction_Card{character}Label"] = {"recognition": "Or", "any_of": label_conditions(x)}
        overrides[f"StealInteraction_Card{character}Select"] = {
            "target": [centers[slot], 610, 1, 1]
        }
    return overrides


def _capture_client_frame(context):
    tasker = getattr(context, "tasker", None)
    if tasker is None or getattr(tasker, "stopping", False):
        return None
    controller = getattr(tasker, "controller", None)
    if controller is None or not hasattr(controller, "post_screencap"):
        return None
    job = controller.post_screencap()
    deadline = time.monotonic() + 5.0
    while not job.done:
        if getattr(tasker, "stopping", False) or time.monotonic() >= deadline:
            return None
        time.sleep(0.05)
    if not job.succeeded:
        return None
    image = job.get()
    if image is None or not getattr(image, "size", 0) or getattr(image, "shape", ())[:2] != (720, 1280):
        return None
    return image


def _recover_failed_interaction(context):
    """Best-effort dismissal of a leftover stealing preview or NPC menu on failure.

    Never converts a failed interaction into success and never writes any
    completion record; only prevents a stuck modal from blocking later routes.
    """
    tasker = getattr(context, "tasker", None)
    if tasker is None or getattr(tasker, "stopping", False):
        return
    if not hasattr(context, "run_recognition") or not hasattr(context, "run_action"):
        return
    try:
        frame = _capture_client_frame(context)
        if frame is None:
            return
        for _ in range(2):
            preview = context.run_recognition("StealInteraction_PreviewPresent", frame)
            if not (preview and preview.hit):
                break
            cancel = context.run_recognition("StealInteraction_PreviewCancel", frame)
            if not (cancel and cancel.hit and cancel.box is not None):
                break
            x, y, width, height = list(cancel.box)
            if width <= 0 or height <= 0:
                break
            point = [int(x + width / 2), int(y + height / 2), 1, 1]
            context.run_action(
                "StealInteraction_Click",
                pipeline_override={"StealInteraction_Click": {"target": point}},
            )
            time.sleep(0.6)
            frame = _capture_client_frame(context)
            if frame is None:
                return
        preview = context.run_recognition("StealInteraction_PreviewPresent", frame)
        if preview and preview.hit:
            return
        heading = context.run_recognition("StealInteraction_TalentHeading", frame)
        if heading and heading.hit:
            context.run_action("StealInteraction_CloseNpc")
            time.sleep(0.8)
    except Exception as exc:
        mfaalog.error(f"[偷窃] 失败后清理交互界面异常：{exc}")


@AgentServer.custom_action("StealInteraction")
class StealInteractionAction(CustomAction):
    def run(self, context, argv) -> bool:
        local = None
        try:
            raw = argv.custom_action_param
            params = json.loads(raw) if isinstance(raw, str) else raw
            overrides = interaction_overrides(params)
            local = context.clone()
            detected = local.run_task("StealInteraction_DetectEntry", overrides)
            if detected is None or not detected.status.succeeded:
                raise ValueError("NPC or skill-card layout was not confirmed")
            layouts = [offset for offset in (0, 1)
                       if any(node.name == f"StealInteraction_Offset{offset}" for node in detected.nodes)]
            if len(layouts) != 1:
                raise ValueError("Skill-card layout was ambiguous")
            overrides.update(card_layout_overrides(layouts[0]))
            result = local.run_task("StealInteraction_Entry", overrides)
            if (result is None or not result.status.succeeded
                    or not any(node.name == "StealInteraction_AllCardsOnCooldown"
                               for node in result.nodes)):
                _recover_failed_interaction(local)
                mfaalog.error("[偷窃] 未确认当前NPC的三个角色均完成，不写入周完成记录。")
                return False
            mfaalog.info(f"[偷窃] {params['npc_names'][0]}：三个角色均有明确周倒计时。")
            return True
        except Exception as exc:
            if local is not None:
                _recover_failed_interaction(local)
            mfaalog.error(f"[偷窃] 交互验证失败：{exc}")
            return False
