"""本次出售请求的进程内记录；金额确认与一次库存回读不得重复记账。"""

from dataclasses import dataclass, field


def integer(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label}必须是非负整数")
    return value


@dataclass
class SaleBatch:
    request: dict
    account_id: str
    day: str
    status: str = "new"
    reason: str = ""
    ready: dict | None = None
    pending: dict | None = None
    retry_used: bool = False
    uncertain: bool = False
    actual_quantity: int = 0
    inventory_after: int | None = None
    proofs: list = field(default_factory=list)

    def arm(self, ready):
        if self.status not in ("new", "reconciled"):
            raise ValueError("本批不是可准备出售的状态")
        inventory = integer(ready["inventory"], "售前库存")
        selected = integer(ready["selected"], "本批选量")
        quoted = integer(ready["quoted_total"], "本批报价")
        if not 0 < selected <= inventory or quoted <= 0:
            raise ValueError("本批库存、选量或报价无效")
        self.ready = dict(ready)
        self.status = "ready"
        # 准备后到框架报告 Selling 之间也可能中断，不能默认没发生交易。
        self.uncertain = True

    def confirm(self, proof):
        if self.status != "ready" or self.ready is None:
            raise ValueError("没有待确认的出售批次")
        before = integer(proof["before"], "售前金币")
        after = integer(proof["after"], "售后金币")
        expected = self.ready["quoted_total"]
        if after - before != expected or proof.get("delta") != expected:
            raise ValueError("金币差额与本批报价不符")
        self.actual_quantity += self.ready["selected"]
        self.inventory_after = self.ready["inventory"] - self.ready["selected"]
        self.proofs.append({"basis": "quoted_gold", **self.ready, "gold": dict(proof)})
        self.status, self.uncertain = "confirmed", False

    def retry(self):
        if self.status != "ready" or self.ready is None or self.retry_used:
            self.fail("金额核对未通过，本批一次补试已结束")
            return False
        self.pending = self.ready
        self.retry_used = True
        self.status = "rechecking"
        return True

    def reconcile(self, inventory):
        if self.status != "rechecking" or self.pending is None:
            raise ValueError("没有待回读的上一笔")
        inventory = integer(inventory, "回读库存")
        sold = self.pending["inventory"] - inventory
        if not 0 <= sold <= self.pending["selected"]:
            raise ValueError("库存变化与上一笔选量矛盾")
        remaining = self.pending["selected"] - sold
        self.actual_quantity += sold
        self.inventory_after = inventory
        self.proofs.append({"basis": "inventory_delta", "before": self.pending["inventory"],
                            "after": inventory, "quantity": sold})
        self.pending = None
        self.ready = None
        self.status = "reconciled" if remaining else "confirmed"
        self.uncertain = False
        return remaining

    def absent(self):
        if self.status == "rechecking":
            self.reconcile(0)
        elif self.status == "new":
            self.status, self.inventory_after = "skipped", 0
            self.reason = "从柜台列表顶部查至末端，确认物品为0"
        else:
            raise ValueError("当前状态不允许用列表缺失判零")

    def fail(self, reason):
        self.reason = str(reason)
        self.status = "unknown" if self.uncertain else "rejected"

    def result(self):
        return {"status": self.status, "reason": self.reason, "actual_quantity": self.actual_quantity,
                "inventory_after": None if self.uncertain else self.inventory_after,
                "uncertain": self.uncertain, "retry_used": self.retry_used,
                "proofs": list(self.proofs)}


_BATCHES: dict[str, SaleBatch] = {}


def put_batch(batch):
    key = batch.request["request_id"]
    if not isinstance(key, str) or not key or key in _BATCHES:
        raise ValueError("出售请求编号无效或重复")
    _BATCHES[key] = batch


def get_batch(request_id):
    if not isinstance(request_id, str) or request_id not in _BATCHES:
        raise ValueError("本次出售请求记录不存在")
    return _BATCHES[request_id]


def take_batch(request_id):
    return _BATCHES.pop(request_id, None)
