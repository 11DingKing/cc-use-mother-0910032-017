"""采购订单变更控制的领域模型。

模型覆盖 ``domain/contract.json`` 声明的关键约束：
订单基线版本、部分变更接受、并发乐观锁、差异责任追踪。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional


def utcnow() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(timezone.utc)


class Party(str, Enum):
    """变更发起方。"""

    BUYER = "采购方"
    SUPPLIER = "供应商"


class Responsibility(str, Enum):
    """差异责任方。"""

    BUYER = "采购方"
    SUPPLIER = "供应商"
    NONE = "无"


class LineField(str, Enum):
    """允许变更的订单行字段。"""

    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    DELIVERY_DATE = "delivery_date"
    DELIVERY_LOCATION = "delivery_location"


class ProposalStatus(str, Enum):
    """变更提案状态。"""

    PENDING = "待确认"
    ACCEPTED = "已接受"
    PARTIALLY_ACCEPTED = "部分接受"
    REJECTED = "已拒绝"
    APPLIED = "已生效"


class ItemDecision(str, Enum):
    """单个变更项的确认结论。"""

    PENDING = "待确认"
    ACCEPTED = "已接受"
    REJECTED = "已拒绝"


class ConfirmationStatus(str, Enum):
    """供应商确认（签署）状态。"""

    SIGNED = "已签署"
    WITHDRAWN = "已撤回"


class CommitmentKind(str, Enum):
    """供应商受影响承诺的类型。"""

    MATERIAL_PREPARED = "已备料"
    PRODUCTION_SCHEDULED = "已排产"
    CAPACITY_RESERVED = "已预留产能"


@dataclass(frozen=True)
class OrderLine:
    """订单行。数量、单价用 Decimal 保证金额精确。"""

    line_id: str
    sku: str
    quantity: Decimal
    unit_price: Decimal
    delivery_date: str  # ISO 日期，如 2026-11-01
    delivery_location: str

    def value_of(self, name: LineField) -> str:
        """按字段名取值的字符串形式，用于差异比较。"""
        return str(getattr(self, name.value))


@dataclass(frozen=True)
class OrderVersion:
    """订单版本：基线为第 1 版，每次变更生效追加一版，形成版本链。"""

    order_id: str
    version_no: int
    parent_version_no: Optional[int]
    lines: tuple[OrderLine, ...]
    cause: str  # "基线" 或 "变更生效"
    source_proposal_id: Optional[str]
    created_by: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ChangeItem:
    """单个变更项：一行一字段的旧值/新值、确认结论与责任评估。"""

    item_id: str
    line_id: str
    field: LineField
    old_value: str
    new_value: str
    decision: ItemDecision = ItemDecision.PENDING
    responsibility: Optional[Responsibility] = None
    claim_amount: Decimal = Decimal("0")
    assessment_reason: str = ""


@dataclass
class SupplierConfirmation:
    """供应商对提案某次修订的签署确认；撤回后保留审计痕迹。"""

    confirmation_id: str
    proposal_id: str
    proposal_revision: int
    decisions: dict[str, ItemDecision]
    signed_by: str
    signed_at: datetime = field(default_factory=utcnow)
    status: ConfirmationStatus = ConfirmationStatus.SIGNED
    withdrawn_by: Optional[str] = None
    withdrawn_at: Optional[datetime] = None


@dataclass
class ChangeProposal:
    """变更提案：基于某一订单版本发起，revision 用于乐观锁。"""

    proposal_id: str
    order_id: str
    base_version_no: int
    initiator: Party
    created_by: str
    items: list[ChangeItem]
    revision: int = 1
    status: ProposalStatus = ProposalStatus.PENDING
    confirmations: list[SupplierConfirmation] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Commitment:
    """受影响承诺：供应商已备料/排产/预留产能的数量与单位成本。"""

    commitment_id: str
    order_id: str
    line_id: str
    kind: CommitmentKind
    quantity: Decimal
    unit_cost: Decimal
    recorded_by: str
    note: str = ""
    recorded_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class CostEvidence:
    """成本证据：支撑索赔判定的单据（金额 + 附件）。"""

    evidence_id: str
    order_id: str
    line_id: Optional[str]
    amount: Decimal
    description: str
    attachment_uri: str
    submitted_by: str
    currency: str = "CNY"
    submitted_at: datetime = field(default_factory=utcnow)
