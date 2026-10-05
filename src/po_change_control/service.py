"""采购订单变更控制的核心服务。

覆盖契约不变量：订单基线版本、部分变更接受、并发乐观锁、差异责任追踪。
变更生效时只把「已接受且签署有效」的变更项写入新版本，被拒绝或签署被
撤回的项保持原值，从而避免邮件确认把供应商已备料部分一起覆盖。
"""
from __future__ import annotations

import dataclasses
import threading
from typing import Any

from . import diff
from .errors import ConcurrencyError, NotFoundError, StateError, ValidationError
from .models import (
    CHANGEABLE_FIELDS,
    ChangeItem,
    ChangeProposal,
    Commitment,
    CommitmentKind,
    CostEvidence,
    Decision,
    Order,
    OrderLine,
    OrderStatus,
    ProposalStatus,
    Revision,
    RevisionKind,
    Signature,
    utc_now,
)
from .store import Store

_NUMERIC_FIELDS = ("quantity", "unit_price")

#: 改变订单内容的版本节点类型；拒绝与撤回只是审计节点，不影响内容基线。
_CONTENT_KINDS = (RevisionKind.BASELINE, RevisionKind.CHANGE_APPLIED)


class ChangeControlService:
    """订单基线、变更提案、确认签署、承诺证据与版本链的应用服务。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 订单与基线
    # ------------------------------------------------------------------
    def create_order(
        self,
        supplier_id: str,
        lines: list[dict],
        created_by: str,
        order_id: str | None = None,
    ) -> dict:
        """创建草拟订单。"""
        with self._lock:
            oid = order_id or self.store.next_id("PO")
            if oid in self.store.orders:
                raise ValidationError(f"订单 {oid} 已存在")
            if not lines:
                raise ValidationError("订单至少包含一行")
            order_lines = [OrderLine.from_dict(item) for item in lines]
            line_nos = [line.line_no for line in order_lines]
            if len(line_nos) != len(set(line_nos)):
                raise ValidationError("订单行号不能重复")
            order = Order(order_id=oid, supplier_id=supplier_id, lines=order_lines, created_by=created_by)
            self.store.orders[oid] = order
            self.store.revisions[oid] = []
            return self.get_order(oid)

    def release_order(self, order_id: str, released_by: str) -> dict:
        """下达订单，生成订单基线版本（版本链第 0 号节点）。"""
        with self._lock:
            order = self._order(order_id)
            if order.status != OrderStatus.DRAFT:
                raise StateError("只有草拟状态的订单可以下达")
            order.status = OrderStatus.RELEASED
            revision = self._append_revision(
                order_id=order_id,
                kind=RevisionKind.BASELINE,
                lines=order.lines,
                proposal_id=None,
                note="订单基线",
                created_by=released_by,
            )
            return revision.to_dict()

    # ------------------------------------------------------------------
    # 变更提案与供应商确认
    # ------------------------------------------------------------------
    def submit_proposal(
        self,
        order_id: str,
        items: list[dict],
        reason: str,
        proposed_by: str,
        base_seq: int | None = None,
    ) -> dict:
        """基于最新生效版本提交变更提案，进入待确认状态。"""
        with self._lock:
            order = self._order(order_id)
            if order.status not in (OrderStatus.RELEASED, OrderStatus.FULFILLING):
                raise StateError("订单未下达，不能提交变更提案")
            head_seq = self._content_head(order_id).seq
            base_seq = head_seq if base_seq is None else base_seq
            if base_seq != head_seq:
                raise ConcurrencyError(f"提案必须基于最新版本 {head_seq}，而不是 {base_seq}")
            if not items:
                raise ValidationError("提案至少包含一项变更")
            base_lines = {line.line_no: line for line in self._revision(order_id, base_seq).lines}
            change_items = [self._build_item(raw, base_lines) for raw in items]
            proposal = ChangeProposal(
                proposal_id=self.store.next_id("CP"),
                order_id=order_id,
                base_seq=base_seq,
                reason=reason,
                proposed_by=proposed_by,
                items=change_items,
            )
            self.store.proposals[proposal.proposal_id] = proposal
            return proposal.to_dict()

    def confirm_proposal(
        self,
        proposal_id: str,
        decisions: dict[str, str],
        signed_by: str,
        expected_lock_version: int,
    ) -> dict:
        """供应商逐项确认（accept/reject）并签署，乐观锁防止并发覆盖。"""
        with self._lock:
            proposal = self._proposal(proposal_id)
            self._check_lock(proposal, expected_lock_version)
            if proposal.status != ProposalStatus.SUBMITTED:
                raise StateError(f"提案当前状态为{proposal.status.value}，不能继续确认")
            if not decisions:
                raise ValidationError("确认决定不能为空")
            item_map = {item.item_id: item for item in proposal.items}
            for item_id, decision in decisions.items():
                item = item_map.get(item_id)
                if item is None:
                    raise NotFoundError(f"变更项 {item_id} 不存在")
                if item.decision != Decision.PENDING:
                    raise StateError(f"变更项 {item_id} 已确认，不能重复决定")
                if decision == "accept":
                    item.decision = Decision.ACCEPTED
                    item.signature = Signature(signed_by=signed_by, signed_at=utc_now())
                elif decision == "reject":
                    item.decision = Decision.REJECTED
                else:
                    raise ValidationError("决定必须是 accept 或 reject")
            proposal.lock_version += 1
            self._refresh_status(proposal)
            if proposal.status == ProposalStatus.REJECTED and not proposal.rejection_recorded:
                proposal.rejection_recorded = True
                self._append_revision(
                    order_id=proposal.order_id,
                    kind=RevisionKind.CHANGE_REJECTED,
                    lines=self._content_head(proposal.order_id).lines,
                    proposal_id=proposal.proposal_id,
                    note="供应商全部拒绝，订单内容不变",
                    created_by=signed_by,
                    details=[item.to_dict() for item in proposal.items],
                )
            return proposal.to_dict()

    def withdraw_signature(
        self,
        proposal_id: str,
        item_id: str,
        withdrawn_by: str,
        expected_lock_version: int,
    ) -> dict:
        """撤回某项签署：签署失效、变更项回到待确认，撤回事件写入版本链。"""
        with self._lock:
            proposal = self._proposal(proposal_id)
            self._check_lock(proposal, expected_lock_version)
            if proposal.status == ProposalStatus.APPLIED:
                raise StateError("提案已生效，签署不可撤回，请提交冲销提案")
            item = next((i for i in proposal.items if i.item_id == item_id), None)
            if item is None:
                raise NotFoundError(f"变更项 {item_id} 不存在")
            if item.signature is None or item.signature.withdrawn:
                raise StateError("该变更项没有有效签署")
            item.signature.withdrawn = True
            item.signature.withdrawn_by = withdrawn_by
            item.signature.withdrawn_at = utc_now()
            item.decision = Decision.PENDING
            proposal.lock_version += 1
            self._refresh_status(proposal)
            self._append_revision(
                order_id=proposal.order_id,
                kind=RevisionKind.SIGNATURE_WITHDRAWN,
                lines=self._content_head(proposal.order_id).lines,
                proposal_id=proposal.proposal_id,
                note=f"撤回变更项 {item_id} 的签署，订单内容不变",
                created_by=withdrawn_by,
                details=[item.to_dict()],
            )
            return proposal.to_dict()

    def apply_proposal(self, proposal_id: str, applied_by: str, expected_lock_version: int) -> dict:
        """变更生效：只把被批准（接受且签署有效）的变更项写入新版本。"""
        with self._lock:
            proposal = self._proposal(proposal_id)
            self._check_lock(proposal, expected_lock_version)
            if proposal.status not in (ProposalStatus.ACCEPTED, ProposalStatus.PARTIALLY_ACCEPTED):
                raise StateError("只有被接受（含部分接受）的提案可以生效")
            head = self._content_head(proposal.order_id)
            if proposal.base_seq != head.seq:
                raise ConcurrencyError(
                    f"提案基于版本 {proposal.base_seq}，当前最新版本为 {head.seq}，请重新基于最新版本提案"
                )
            approved = [
                item
                for item in proposal.items
                if item.decision == Decision.ACCEPTED and item.signature and not item.signature.withdrawn
            ]
            if not approved:
                raise StateError("没有已批准且签署有效的变更项")
            new_lines = [dataclasses.replace(line) for line in head.lines]
            line_map = {line.line_no: line for line in new_lines}
            for item in approved:
                setattr(line_map[item.line_no], item.field, item.new_value)
            self._append_revision(
                order_id=proposal.order_id,
                kind=RevisionKind.CHANGE_APPLIED,
                lines=new_lines,
                proposal_id=proposal.proposal_id,
                note=proposal.reason,
                created_by=applied_by,
                details=[item.to_dict() for item in proposal.items],
            )
            order = self._order(proposal.order_id)
            order.lines = new_lines
            order.status = OrderStatus.FULFILLING
            proposal.status = ProposalStatus.APPLIED
            proposal.lock_version += 1
            return proposal.to_dict()

    # ------------------------------------------------------------------
    # 受影响承诺与成本证据
    # ------------------------------------------------------------------
    def add_commitment(
        self,
        order_id: str,
        line_no: str,
        kind: str,
        quantity: int,
        note: str = "",
        created_by: str = "供应商",
    ) -> dict:
        """登记受影响承诺，如供应商已备料数量。"""
        with self._lock:
            order = self._order(order_id)
            if order.status not in (OrderStatus.RELEASED, OrderStatus.FULFILLING):
                raise StateError("订单未下达，不能登记承诺")
            if line_no not in {line.line_no for line in order.lines}:
                raise ValidationError(f"订单行 {line_no} 不存在")
            try:
                kind_enum = CommitmentKind(kind)
            except ValueError:
                raise ValidationError(f"未知的承诺类型：{kind}") from None
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                raise ValidationError("承诺数量必须是正整数")
            commitment = Commitment(
                commitment_id=self.store.next_id("CM"),
                order_id=order_id,
                line_no=line_no,
                kind=kind_enum,
                quantity=quantity,
                note=note,
                created_by=created_by,
            )
            self.store.commitments[commitment.commitment_id] = commitment
            return commitment.to_dict()

    def add_evidence(
        self,
        commitment_id: str,
        amount: float,
        currency: str = "CNY",
        description: str = "",
        doc_ref: str = "",
        submitted_by: str = "供应商",
    ) -> dict:
        """为承诺登记成本证据，作为索赔依据。"""
        with self._lock:
            if commitment_id not in self.store.commitments:
                raise NotFoundError(f"承诺 {commitment_id} 不存在")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                raise ValidationError("证据金额必须是正数")
            evidence = CostEvidence(
                evidence_id=self.store.next_id("CE"),
                commitment_id=commitment_id,
                amount=amount,
                currency=currency,
                description=description,
                doc_ref=doc_ref,
                submitted_by=submitted_by,
            )
            self.store.evidences[evidence.evidence_id] = evidence
            return evidence.to_dict()

    # ------------------------------------------------------------------
    # 查询与版本比较
    # ------------------------------------------------------------------
    def get_order(self, order_id: str) -> dict:
        """返回订单当前生效内容。"""
        with self._lock:
            order = self._order(order_id)
            data = order.to_dict()
            chain = self.store.revisions[order_id]
            data["head_seq"] = chain[-1].seq if chain else None
            return data

    def get_proposal(self, proposal_id: str) -> dict:
        with self._lock:
            return self._proposal(proposal_id).to_dict()

    def list_proposals(self, order_id: str) -> list[dict]:
        with self._lock:
            self._order(order_id)
            return [p.to_dict() for p in self.store.proposals.values() if p.order_id == order_id]

    def list_versions(self, order_id: str) -> list[dict]:
        """版本链摘要：基线、生效、拒绝、撤回节点按序排列。"""
        with self._lock:
            self._order(order_id)
            return [rev.to_dict(with_lines=False) for rev in self.store.revisions[order_id]]

    def get_version(self, order_id: str, seq: int) -> dict:
        """指定版本的完整快照。"""
        with self._lock:
            return self._revision(order_id, seq).to_dict()

    def diff_versions(self, order_id: str, from_seq: int, to_seq: int) -> dict:
        """比较任意两个版本，逐项展示差异、责任方与索赔依据。"""
        with self._lock:
            return diff.compute_diff(self, order_id, from_seq, to_seq)

    # ------------------------------------------------------------------
    # 供 diff 模块与测试使用的查询
    # ------------------------------------------------------------------
    def revision_of(self, order_id: str, seq: int) -> Revision:
        return self._revision(order_id, seq)

    def chain_of(self, order_id: str) -> list[Revision]:
        self._order(order_id)
        return list(self.store.revisions[order_id])

    def proposal_of(self, proposal_id: str) -> ChangeProposal:
        return self._proposal(proposal_id)

    def commitments_for(self, order_id: str, line_no: str) -> list[Commitment]:
        return [
            c for c in self.store.commitments.values() if c.order_id == order_id and c.line_no == line_no
        ]

    def evidences_for(self, commitment_id: str) -> list[CostEvidence]:
        return [e for e in self.store.evidences.values() if e.commitment_id == commitment_id]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _order(self, order_id: str) -> Order:
        order = self.store.orders.get(order_id)
        if order is None:
            raise NotFoundError(f"订单 {order_id} 不存在")
        return order

    def _proposal(self, proposal_id: str) -> ChangeProposal:
        proposal = self.store.proposals.get(proposal_id)
        if proposal is None:
            raise NotFoundError(f"变更提案 {proposal_id} 不存在")
        return proposal

    def _revision(self, order_id: str, seq: int) -> Revision:
        self._order(order_id)
        chain = self.store.revisions[order_id]
        if not isinstance(seq, int) or seq < 0 or seq >= len(chain):
            raise NotFoundError(f"订单 {order_id} 没有版本 {seq}")
        return chain[seq]

    def _content_head(self, order_id: str) -> Revision:
        """最新内容版本：跳过拒绝、撤回等只留痕不改内容的审计节点。"""
        for revision in reversed(self.store.revisions[order_id]):
            if revision.kind in _CONTENT_KINDS:
                return revision
        raise StateError("订单尚未下达，没有版本")

    @staticmethod
    def _check_lock(proposal: ChangeProposal, expected_lock_version: int) -> None:
        if proposal.lock_version != expected_lock_version:
            raise ConcurrencyError(
                f"提案锁版本冲突：期望 {expected_lock_version}，实际 {proposal.lock_version}，请刷新后重试"
            )

    def _build_item(self, raw: dict, base_lines: dict[str, OrderLine]) -> ChangeItem:
        line_no = raw.get("line_no")
        field_name = raw.get("field")
        new_value = raw.get("new_value")
        if field_name not in CHANGEABLE_FIELDS:
            raise ValidationError(f"字段 {field_name!r} 不允许变更，可变更字段：{'、'.join(CHANGEABLE_FIELDS)}")
        line = base_lines.get(line_no)
        if line is None:
            raise ValidationError(f"订单行 {line_no} 不存在")
        if field_name in _NUMERIC_FIELDS:
            if not isinstance(new_value, (int, float)) or isinstance(new_value, bool):
                raise ValidationError(f"字段 {field_name} 的新值必须是数字")
        elif not isinstance(new_value, str):
            raise ValidationError(f"字段 {field_name} 的新值必须是字符串")
        old_value = getattr(line, field_name)
        if new_value == old_value:
            raise ValidationError(f"订单行 {line_no} 的 {field_name} 新值与现值相同")
        return ChangeItem(
            item_id=self.store.next_id("CI"),
            line_no=line_no,
            field=field_name,
            old_value=old_value,
            new_value=new_value,
        )

    @staticmethod
    def _refresh_status(proposal: ChangeProposal) -> None:
        if proposal.status == ProposalStatus.APPLIED:
            return
        if any(item.decision == Decision.PENDING for item in proposal.items):
            proposal.status = ProposalStatus.SUBMITTED
            return
        accepted = sum(1 for item in proposal.items if item.decision == Decision.ACCEPTED)
        if accepted == len(proposal.items):
            proposal.status = ProposalStatus.ACCEPTED
        elif accepted == 0:
            proposal.status = ProposalStatus.REJECTED
        else:
            proposal.status = ProposalStatus.PARTIALLY_ACCEPTED

    def _append_revision(
        self,
        order_id: str,
        kind: RevisionKind,
        lines: list[OrderLine],
        proposal_id: str | None,
        note: str,
        created_by: str,
        details: list[dict] | None = None,
    ) -> Revision:
        chain = self.store.revisions[order_id]
        revision = Revision(
            seq=len(chain),
            order_id=order_id,
            kind=kind,
            lines=[dataclasses.replace(line) for line in lines],
            parent_seq=len(chain) - 1 if chain else None,
            proposal_id=proposal_id,
            note=note,
            created_by=created_by,
            details=details or [],
        )
        chain.append(revision)
        return revision
