"""变更责任判定：确定性纯函数，签署确认时对每个被接受项评估一次。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .models import ChangeItem, Commitment, CostEvidence, LineField, Party, Responsibility

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Assessment:
    """单个变更项的责任结论。"""

    responsibility: Responsibility
    claim_amount: Decimal
    reason: str


def assess_item(
    item: ChangeItem,
    initiator: Party,
    commitments: Sequence[Commitment],
    evidences: Sequence[CostEvidence],
) -> Assessment:
    """评估被接受变更项的责任方与索赔金额。

    规则：
    1. 供应商发起的变更，责任归供应商；采购方已登记成本证据的，证据金额计入索赔。
    2. 采购方发起数量调减：供应商在原订单数量以内已承诺备货、超出新数量的部分，
       按承诺加权单位成本计价，由采购方承担（超出原订单数量的备货由供应商自担）。
    3. 采购方发起的其他变更，凡订单行已登记成本证据的，证据金额由采购方承担。
    4. 以上均不涉及的，视为无责任差异。
    """
    line_commitments = [c for c in commitments if c.line_id == item.line_id]
    evidence_total = sum((e.amount for e in evidences if e.line_id == item.line_id), ZERO)

    if initiator is Party.SUPPLIER:
        reason = "供应商发起的变更，责任归供应商"
        if evidence_total > ZERO:
            reason += "；采购方已登记成本证据，计入索赔"
        return Assessment(Responsibility.SUPPLIER, evidence_total, reason)

    claim = ZERO
    reasons: list[str] = []
    if item.field is LineField.QUANTITY:
        old_qty = Decimal(item.old_value)
        new_qty = Decimal(item.new_value)
        if new_qty < old_qty and line_commitments:
            committed_qty = sum((c.quantity for c in line_commitments), ZERO)
            covered = min(committed_qty, old_qty)
            exposed = max(ZERO, covered - new_qty)
            if exposed > ZERO:
                total_cost = sum((c.quantity * c.unit_cost for c in line_commitments), ZERO)
                avg_cost = total_cost / committed_qty
                claim += exposed * avg_cost
                reasons.append(
                    f"数量 {old_qty}→{new_qty}，供应商已承诺备货 {committed_qty}，"
                    f"受保护差额 {exposed} 按加权成本 {avg_cost} 计价"
                )
    if evidence_total > ZERO:
        claim += evidence_total
        reasons.append(f"订单行已登记成本证据 {evidence_total}")

    if claim > ZERO:
        return Assessment(Responsibility.BUYER, claim.quantize(CENT), "；".join(reasons) + "，由采购方承担")
    return Assessment(Responsibility.NONE, ZERO, "变更未触及供应商承诺且无成本证据")
