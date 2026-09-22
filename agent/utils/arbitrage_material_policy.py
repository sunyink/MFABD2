"""材料出售保留配置：关闭、按天数、手动数量；只读取配置，不执行交易。"""

from dataclasses import dataclass

from .name_i18n import canon


KEEP_ALL = -1
MODE_NODE = "Agt_Arbitrage_MaterialSale_Mode"
AUTO_NODE = "Agt_Arbitrage_MaterialSale_Auto"
MANUAL_NODE = "Agt_Arbitrage_MaterialSale_Manual"
DATA_NODE = "Agt_Arbitrage_MaterialSale_Data"


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def scale_reserve(daily, days):
    """无限保留先分支处理：-1×0仍然是无限保留，而不是零。"""
    _integer(daily, "日保留值", KEEP_ALL)
    _integer(days, "保留天数")
    return KEEP_ALL if daily == KEEP_ALL else daily * days


def _quantities(raw, label):
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{label} 必须是非空材料数量表")
    quantities = {}
    for raw_name, value in raw.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(f"{label} 包含无效材料名")
        material = canon(raw_name.strip())
        if material in quantities:
            raise ValueError(f"{label} 材料名重复: {material}")
        quantities[material] = _integer(value, f"{label}/{material}", KEEP_ALL)
    return quantities


def _attach(context, node_name):
    node = context.get_node_object(node_name)
    attach = getattr(node, "attach", None) if node is not None else None
    if not isinstance(attach, dict):
        raise ValueError(f"材料出售配置缺失: {node_name}")
    return attach


@dataclass
class MaterialReservePolicy:
    mode: str
    reserves: dict[str, int]
    sale_eligible: frozenset[str]

    def reserve_for(self, material):
        """未收录或未确认可直卖的材料，始终无限保留。"""
        material = canon(material)
        if self.mode == "off" or material not in self.sale_eligible:
            return KEEP_ALL
        return self.reserves.get(material, KEEP_ALL)


def read_material_reserve_policy(context):
    """读取当前模式使用的配置；非法数据抛错，由出售主控跳过材料分支。

    不读取账号存档、不复算日表、不修改节点或界面值。返回值只供最终材料
    出售计算数量使用，不能把有限保留值当作价格或交易成功的证据。
    """
    mode = _attach(context, MODE_NODE).get("mode")
    if mode not in ("off", "auto", "manual"):
        raise ValueError(f"未知材料出售模式: {mode!r}")
    if mode == "off":
        return MaterialReservePolicy(mode, {}, frozenset())

    data = _attach(context, DATA_NODE)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("材料日表版本不支持")
    daily = _quantities(data.get("daily_reserve"), "日保留表")
    raw_eligible = data.get("sale_eligible")
    if not isinstance(raw_eligible, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_eligible
    ):
        raise ValueError("材料直卖资格表无效")
    eligible = frozenset(canon(item.strip()) for item in raw_eligible)
    if len(eligible) != len(raw_eligible) or not eligible <= daily.keys():
        raise ValueError("材料直卖资格表重复或包含未知材料")

    if mode == "auto":
        days = _integer(_attach(context, AUTO_NODE).get("days"), "保留天数")
        reserves = {material: scale_reserve(value, days) for material, value in daily.items()}
    else:
        reserves = _quantities(_attach(context, MANUAL_NODE), "手动保留表")
        if reserves.keys() != daily.keys():
            raise ValueError("手动保留表与材料目录不一致")
    if any(value != KEEP_ALL for material, value in reserves.items() if material not in eligible):
        raise ValueError("未确认直卖资格的材料必须无限保留")
    return MaterialReservePolicy(mode, reserves, eligible)
