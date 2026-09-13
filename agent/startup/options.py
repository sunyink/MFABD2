"""PC options shared by PI pretask arguments and task-local pipeline attach."""

import json
from dataclasses import dataclass

from .common import PreparationError

NODE = "StartGame_PCWindowOptions"
RESOLUTION_OPTION = "PC窗口分辨率"
MINIMIZE_OPTION = "PC启动最小化"
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}


@dataclass(frozen=True)
class PCOptions:
    resolution: str = "720p"
    minimize: bool = False

    def __post_init__(self):
        if self.resolution not in RESOLUTIONS:
            raise PreparationError(f"不支持的 PC 窗口分辨率：{self.resolution}")
        if not isinstance(self.minimize, bool):
            raise PreparationError("PC 最小化配置必须为布尔值")

    @property
    def target(self):
        return RESOLUTIONS[self.resolution]

    @classmethod
    def from_pretask(cls, arguments):
        if not arguments:
            return cls()
        if len(arguments) != 1:
            raise PreparationError("PC pretask 需要一个选项 JSON 参数")
        try:
            values = json.loads(arguments[0])
            if not isinstance(values, dict):
                raise ValueError("选项不是对象")
            minimized = values.get(MINIMIZE_OPTION, "No")
            if minimized not in ("Yes", "No"):
                raise ValueError("最小化选项必须为 Yes/No")
            return cls(values.get(RESOLUTION_OPTION, "720p"), minimized == "Yes")
        except (ValueError, TypeError) as exc:
            raise PreparationError(f"PC pretask 选项无效：{exc}") from exc

    @classmethod
    def from_context(cls, context):
        node = context.get_node_object(NODE)
        if node is None:
            raise PreparationError("缺少 PC 窗口配置节点，请更新 base 资源")
        values = node.attach
        if not isinstance(values, dict):
            raise PreparationError("PC 窗口配置 attach 不是对象")
        return cls(values.get("resolution", "720p"), values.get("minimize", False))
