"""Run the three-character stealing UI after the caller has reached an NPC.

The subtask has no persistent completion writes. Its success means all three
visible skill cards carry a recognized weekly countdown for this exact NPC.
"""

import json
import re

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from utils import mfaalog


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
    # Preserve the established HH:MM crop. Detect the day-count text separately
    # so card arrows/lines cannot be read as an extra glyph (for example 5天一).
    return [
        {"recognition": "OCR", "roi": [x + 8, 593, 62, 23],
         "expected": [r"^\d{1,3}[:：][0-5]\d$"], "only_rec": True, "threshold": 0.7},
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


@AgentServer.custom_action("StealInteraction")
class StealInteractionAction(CustomAction):
    def run(self, context, argv) -> bool:
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
                mfaalog.error("[偷窃] 未确认当前NPC的三个角色均完成，不写入周完成记录。")
                return False
            mfaalog.info(f"[偷窃] {params['npc_names'][0]}：三个角色均有明确周倒计时。")
            return True
        except Exception as exc:
            mfaalog.error(f"[偷窃] 交互验证失败：{exc}")
            return False
