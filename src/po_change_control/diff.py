"""任意版本比较与差异责任方追踪。

差异责任方取变更提案的发起方：采购计划员发起归采购方，供应商发起归供应商。
差异行上登记的受影响承诺与成本证据一并输出，作为索赔判断依据。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import ValidationError
from .models import CHANGEABLE_FIELDS, Decision, RevisionKind, responsible_party

if TYPE_CHECKING:
    from .service import ChangeControlService


def compute_diff(service: "ChangeControlService", order_id: str, from_seq: int, to_seq: int) -> dict:
    """比较两个版本快照，逐项给出差异、责任方与索赔依据。"""
    if from_seq >= to_seq:
        raise ValidationError("from_seq 必须小于 to_seq")
    from_rev = service.revision_of(order_id, from_seq)
    to_rev = service.revision_of(order_id, to_seq)

    # 追溯区间 (from_seq, to_seq] 内每次生效变更的来源提案项，同一字段以后生效者为准。
    sources: dict[tuple[str, str], tuple] = {}
    for rev in service.chain_of(order_id):
        if not (from_seq < rev.seq <= to_seq):
            continue
        if rev.kind != RevisionKind.CHANGE_APPLIED or not rev.proposal_id:
            continue
        proposal = service.proposal_of(rev.proposal_id)
        for item in proposal.items:
            if item.decision == Decision.ACCEPTED:
                sources[(item.line_no, item.field)] = (proposal, item)

    from_lines = {line.line_no: line for line in from_rev.lines}
    to_lines = {line.line_no: line for line in to_rev.lines}
    changes = []
    for line_no in sorted(from_lines):
        new_line = to_lines.get(line_no)
        if new_line is None:
            continue
        old_line = from_lines[line_no]
        for field_name in CHANGEABLE_FIELDS:
            old_value = getattr(old_line, field_name)
            new_value = getattr(new_line, field_name)
            if old_value == new_value:
                continue
            entry: dict = {
                "line_no": line_no,
                "field": field_name,
                "old_value": old_value,
                "new_value": new_value,
                "proposal_id": None,
                "reason": None,
                "proposed_by": None,
                "responsible_party": None,
            }
            source = sources.get((line_no, field_name))
            if source is not None:
                proposal, _item = source
                entry["proposal_id"] = proposal.proposal_id
                entry["reason"] = proposal.reason
                entry["proposed_by"] = proposal.proposed_by
                entry["responsible_party"] = responsible_party(proposal.proposed_by)
            commitments = service.commitments_for(order_id, line_no)
            if commitments:
                evidences = [e for c in commitments for e in service.evidences_for(c.commitment_id)]
                entry["affected_commitments"] = [c.to_dict() for c in commitments]
                entry["cost_evidence"] = [e.to_dict() for e in evidences]
                entry["claim_amount_total"] = sum(e.amount for e in evidences)
                entry["claim_against"] = entry["responsible_party"]
            changes.append(entry)
    return {
        "order_id": order_id,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "change_count": len(changes),
        "changes": changes,
    }
