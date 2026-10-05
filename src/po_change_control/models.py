"""采购订单变更控制的领域模型。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """返回 UTC 当前时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat()


class OrderStatus(str, enum.Enum):
    """订单状态，与 domain/contract.json 的 states 对齐。"""

    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    FULFILLING = "履行中"
    CLOSED = "已关闭"


class ProposalStatus(str, enum.Enum):
    """变更提案状态。"""

    SUBMITTED = "待确认"
    PARTIALLY_ACCEPTED = "部分接受"
    ACCEPTED = "全部接受"
    REJECTED = "全部拒绝"
    APPLIED = "已生效"


class Decision(str, enum.Enum):
    """单个变更项的确认决定。"""

    PENDING = "待确认"
    ACCEPTED = "接受"
    REJECTED = "拒绝"


class RevisionKind(str, enum.Enum):
    """版本链节点类型：拒绝、部分接受、连续变更与撤回签署都会落链。"""

    BASELINE = "订单基线"
    CHANGE_APPLIED = "变更生效"
    CHANGE_REJECTED = "变更拒绝"
    SIGNATURE_WITHDRAWN = "签署撤回"


class CommitmentKind(str, enum.Enum):
    """供应商已承诺（受影响承诺）的类型。"""

    MATERIAL_PREPARED = "已备料"
    IN_PRODUCTION = "在制"
    IN_TRANSIT = "在途"
    DELIVERED = "已交付"


#: 允许变更的订单行字段：数量、单价、交期、交付地点。
CHANGEABLE_FIELDS = ("quantity", "unit_price", "delivery_date", "delivery_location")

BUYER_SIDE = "采购方"
SUPPLIER_SIDE = "供应商"
SUPPLIER_ACTOR = "供应商"


def responsible_party(proposed_by: str) -> str:
    """差异责任方即变更发起方：供应商发起归供应商，其余角色发起归采购方。"""
    return SUPPLIER_SIDE if proposed_by == SUPPLIER_ACTOR else BUYER_SIDE


@dataclass
class OrderLine:
    """订单行：数量、交期、交付地点为可变更字段。"""

    line_no: str
    sku: str
    quantity: int
    unit_price: float
    delivery_date: str
    delivery_location: str

    def to_dict(self) -> dict:
        return {
            "line_no": self.line_no,
            "sku": self.sku,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "delivery_date": self.delivery_date,
            "delivery_location": self.delivery_location,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrderLine":
        return cls(
            line_no=str(data["line_no"]),
            sku=str(data["sku"]),
            quantity=data["quantity"],
            unit_price=data["unit_price"],
            delivery_date=str(data["delivery_date"]),
            delivery_location=str(data["delivery_location"]),
        )


@dataclass
class Order:
    """采购订单。lines 始终表示当前生效内容，历史见版本链。"""

    order_id: str
    supplier_id: str
    lines: list[OrderLine]
    created_by: str
    status: OrderStatus = OrderStatus.DRAFT
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "supplier_id": self.supplier_id,
            "status": self.status.value,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass
class Signature:
    """供应商对单个变更项的签署，可撤回并保留审计痕迹。"""

    signed_by: str
    signed_at: str
    withdrawn: bool = False
    withdrawn_by: str | None = None
    withdrawn_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "signed_by": self.signed_by,
            "signed_at": self.signed_at,
            "withdrawn": self.withdrawn,
            "withdrawn_by": self.withdrawn_by,
            "withdrawn_at": self.withdrawn_at,
        }


@dataclass
class ChangeItem:
    """变更项：针对某订单行某字段的一次修改。"""

    item_id: str
    line_no: str
    field: str
    old_value: Any
    new_value: Any
    decision: Decision = Decision.PENDING
    signature: Signature | None = None

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "line_no": self.line_no,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "decision": self.decision.value,
            "signature": self.signature.to_dict() if self.signature else None,
        }


@dataclass
class ChangeProposal:
    """变更提案：一组变更项，lock_version 用于并发确认的乐观锁。"""

    proposal_id: str
    order_id: str
    base_seq: int
    reason: str
    proposed_by: str
    items: list[ChangeItem]
    status: ProposalStatus = ProposalStatus.SUBMITTED
    lock_version: int = 0
    rejection_recorded: bool = False
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "order_id": self.order_id,
            "base_seq": self.base_seq,
            "reason": self.reason,
            "proposed_by": self.proposed_by,
            "status": self.status.value,
            "lock_version": self.lock_version,
            "created_at": self.created_at,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class Revision:
    """版本链节点：每个节点都持有当时的订单行快照，支持任意版本比较。"""

    seq: int
    order_id: str
    kind: RevisionKind
    lines: list[OrderLine]
    parent_seq: int | None
    proposal_id: str | None
    note: str
    created_by: str
    created_at: str = field(default_factory=utc_now)
    details: list[dict] = field(default_factory=list)

    def to_dict(self, with_lines: bool = True) -> dict:
        data = {
            "seq": self.seq,
            "order_id": self.order_id,
            "kind": self.kind.value,
            "parent_seq": self.parent_seq,
            "proposal_id": self.proposal_id,
            "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "details": self.details,
        }
        if with_lines:
            data["lines"] = [line.to_dict() for line in self.lines]
        else:
            data["line_count"] = len(self.lines)
        return data


@dataclass
class Commitment:
    """受影响承诺：供应商已备料/在制/在途/已交付的部分。"""

    commitment_id: str
    order_id: str
    line_no: str
    kind: CommitmentKind
    quantity: int
    note: str
    created_by: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "commitment_id": self.commitment_id,
            "order_id": self.order_id,
            "line_no": self.line_no,
            "kind": self.kind.value,
            "quantity": self.quantity,
            "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass
class CostEvidence:
    """成本证据：承诺已发生成本的凭证，是索赔的依据。"""

    evidence_id: str
    commitment_id: str
    amount: float
    currency: str
    description: str
    doc_ref: str
    submitted_by: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "commitment_id": self.commitment_id,
            "amount": self.amount,
            "currency": self.currency,
            "description": self.description,
            "doc_ref": self.doc_ref,
            "submitted_by": self.submitted_by,
            "created_at": self.created_at,
        }
