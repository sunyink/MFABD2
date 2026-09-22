"""PC options read from the task-local pipeline attach.

pretask 不再消费选项（它只负责拉起游戏并确认主窗口存在），所以这里只剩 sink 这
一个入口。原先按中文 option 名解析 PI 传入 JSON 的 from_pretask 已随之删除。
"""

from dataclasses import dataclass

from .common import PreparationError

NODE = "StartGame_PCWindowOptions"
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
    def from_context(cls, context):
        node = context.get_node_object(NODE)
        if node is None:
            raise PreparationError("缺少 PC 窗口配置节点，请更新 base 资源")
        values = node.attach
        if not isinstance(values, dict):
            raise PreparationError("PC 窗口配置 attach 不是对象")
        return cls(values.get("resolution", "720p"), values.get("minimize", False))
