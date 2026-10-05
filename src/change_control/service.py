"""采购订单变更控制的应用服务层。

职责：
- 保存订单基线、变更提案、供应商确认、受影响承诺与成本证据；
- 变更生效时只把已接受的变更项写入新版本（部分接受）；
- 提案修订号与订单版本号双重乐观锁，防止并发确认互相覆盖；
- 支持沿版本链比较任意两个版本，并给出每项差异的责任方。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from .errors import ConcurrencyError, NotFoundError, StateError, ValidationError
from .models import (
    ChangeItem,
    ChangeProposal,
    Commitment,
    CommitmentKind,
    ConfirmationStatus,
    CostEvidence,
    ItemDecision,
    LineField,
    OrderLine,
    OrderVersion,
    Party,
    ProposalStatus,
    SupplierConfirmation,
    utcnow,
)
from .repository import InMemoryStore
from .responsibility import assess_item

_DECIMAL_FIELDS = {LineField.QUANTITY, LineField.UNIT_PRICE}


def _parse_enum(enum_cls, value, label: str):
    """同时接受枚举成员、成员名（如 "BUYER"）与成员值（如 "采购方"）。"""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls[value]
        except KeyError:
            pass
        try:
            return enum_cls(value)
        except ValueError:
            pass
    raise ValidationError(f"无法识别的{label}：{value!r}")


_DECISION_ALIASES = {
    "ACCEPT": ItemDecision.ACCEPTED,
    "ACCEPTED": ItemDecision.ACCEPTED,
    "REJECT": ItemDecision.REJECTED,
    "REJECTED": ItemDecision.REJECTED,
}


def _parse_decision(value) -> ItemDecision:
    if isinstance(value, ItemDecision):
        decision = value
    elif isinstance(value, str) and value.upper() in _DECISION_ALIASES:
        decision = _DECISION_ALIASES[value.upper()]
    else:
        decision = _parse_enum(ItemDecision, value, "确认结论")
    if decision is ItemDecision.PENDING:
        raise ValidationError("确认结论只能是 ACCEPT 或 REJECT")
    return decision


def _normalize_value(field: LineField, value) -> str:
    """把变更值规范为可比较的字符串形式。"""
    if field in _DECIMAL_FIELDS:
        try:
            return str(Decimal(str(value)))
        except InvalidOperation:
            raise ValidationError(f"数值格式非法：{value!r}") from None
    text = str(value).strip()
    if not text:
        raise ValidationError("变更值不能为空")
    if field is LineField.DELIVERY_DATE:
        try:
            date.fromisoformat(text)
        except ValueError:
            raise ValidationError(f"交期必须是 ISO 日期：{text!r}") from None
    return text


class ChangeControlService:
    """变更控制领域服务，所有方法线程安全。"""

    def __init__(self, store: Optional[InMemoryStore] = None) -> None:
        self.store = store or InMemoryStore()

    # ---------- 订单基线与版本链 ----------

    def create_order_baseline(self, order_id: str, lines, actor: str) -> OrderVersion:
        """创建订单基线（第 1 版）。lines 为 OrderLine 或 dict 列表。"""
        with self.store.lock:
            if order_id in self.store.versions:
                raise ValidationError(f"订单已存在：{order_id}")
            order_lines = tuple(self._parse_line(raw) for raw in lines)
            if not order_lines:
                raise ValidationError("订单至少包含一行")
            line_ids = [line.line_id for line in order_lines]
            if len(line_ids) != len(set(line_ids)):
                raise ValidationError("订单行号不能重复")
            version = OrderVersion(order_id, 1, None, order_lines, "基线", None, actor)
            self.store.versions[order_id] = [version]
            self.store.commitments[order_id] = []
            self.store.evidences[order_id] = []
            return version

    def get_version(self, order_id: str, version_no: int) -> OrderVersion:
        with self.store.lock:
            return self._version(self._chain(order_id), version_no)

    def get_version_chain(self, order_id: str) -> list[OrderVersion]:
        """返回从基线到最新版本的完整版本链。"""
        with self.store.lock:
            return list(self._chain(order_id))

    def get_proposal(self, proposal_id: str) -> ChangeProposal:
        with self.store.lock:
            return self._proposal(proposal_id)

    def list_proposals(self, order_id: str) -> list[ChangeProposal]:
        with self.store.lock:
            self._chain(order_id)
            return sorted(
                (p for p in self.store.proposals.values() if p.order_id == order_id),
                key=lambda p: p.proposal_id,
            )

    # ---------- 受影响承诺与成本证据 ----------

    def register_commitment(
        self, order_id: str, line_id: str, kind, quantity, unit_cost, actor: str, note: str = ""
    ) -> Commitment:
        """登记供应商受影响承诺（已备料/排产/预留产能）。"""
        with self.store.lock:
            chain = self._chain(order_id)
            self._line(chain[-1], line_id)
            kind = _parse_enum(CommitmentKind, kind, "承诺类型")
            qty = self._decimal(quantity, "承诺数量")
            cost = self._decimal(unit_cost, "单位成本")
            if qty <= 0:
                raise ValidationError("承诺数量必须为正")
            if cost < 0:
                raise ValidationError("单位成本不能为负")
            commitment = Commitment(
                self.store.next_id("CM"), order_id, line_id, kind, qty, cost, actor, note
            )
            self.store.commitments[order_id].append(commitment)
            return commitment

    def attach_cost_evidence(
        self,
        order_id: str,
        line_id: Optional[str],
        amount,
        description: str,
        attachment_uri: str,
        actor: str,
        currency: str = "CNY",
    ) -> CostEvidence:
        """登记成本证据（金额 + 附件），可按订单行归集。"""
        with self.store.lock:
            chain = self._chain(order_id)
            if line_id is not None:
                self._line(chain[-1], line_id)
            value = self._decimal(amount, "证据金额")
            if value <= 0:
                raise ValidationError("证据金额必须为正")
            evidence = CostEvidence(
                self.store.next_id("CE"), order_id, line_id, value,
                str(description), str(attachment_uri), actor, str(currency),
            )
            self.store.evidences[order_id].append(evidence)
            return evidence

    # ---------- 变更提案 ----------

    def create_proposal(self, order_id: str, initiator, changes, actor: str) -> ChangeProposal:
        """基于当前最新版本发起变更提案。

        changes: [{"line_id": ..., "field": ..., "new_value": ...}, ...]
        """
        with self.store.lock:
            chain = self._chain(order_id)
            base = chain[-1]
            proposal_id = self.store.next_id("CP")
            items = self._build_items(base, changes, proposal_id)
            proposal = ChangeProposal(
                proposal_id, order_id, base.version_no,
                _parse_enum(Party, initiator, "发起方"), actor, items,
            )
            self.store.proposals[proposal_id] = proposal
            return proposal

    def amend_proposal(self, proposal_id: str, changes, expected_revision: int, actor: str) -> ChangeProposal:
        """修订待确认的提案；修订号 +1，使供应商手上的旧修订号失效。"""
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                raise StateError(f"仅待确认状态可修订提案，当前：{proposal.status.value}")
            self._check_revision(proposal, expected_revision)
            base = self._version(self._chain(proposal.order_id), proposal.base_version_no)
            proposal.items = self._build_items(base, changes, proposal.proposal_id)
            proposal.revision += 1
            return proposal

    def confirm_proposal(
        self, proposal_id: str, decisions: dict, expected_revision: int, actor: str
    ) -> SupplierConfirmation:
        """供应商逐项签署确认（乐观锁：expected_revision 必须是最新修订号）。

        decisions: {item_id: "ACCEPT" | "REJECT"}，必须覆盖全部变更项。
        签署时对每个被接受项做责任评估并随确认固化。
        """
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                raise StateError(f"当前状态不可确认：{proposal.status.value}")
            self._check_revision(proposal, expected_revision)
            parsed = {str(k): _parse_decision(v) for k, v in decisions.items()}
            if set(parsed) != {item.item_id for item in proposal.items}:
                raise ValidationError("确认必须覆盖全部变更项，且不能包含未知项")

            commitments = self.store.commitments[proposal.order_id]
            evidences = self.store.evidences[proposal.order_id]
            for item in proposal.items:
                item.decision = parsed[item.item_id]
                if item.decision is ItemDecision.ACCEPTED:
                    assessment = assess_item(item, proposal.initiator, commitments, evidences)
                    item.responsibility = assessment.responsibility
                    item.claim_amount = assessment.claim_amount
                    item.assessment_reason = assessment.reason

            accepted = sum(1 for i in proposal.items if i.decision is ItemDecision.ACCEPTED)
            if accepted == 0:
                proposal.status = ProposalStatus.REJECTED
            elif accepted == len(proposal.items):
                proposal.status = ProposalStatus.ACCEPTED
            else:
                proposal.status = ProposalStatus.PARTIALLY_ACCEPTED

            confirmation = SupplierConfirmation(
                self.store.next_id("CF"), proposal.proposal_id, proposal.revision, parsed, actor
            )
            proposal.confirmations.append(confirmation)
            return confirmation

    def withdraw_confirmation(self, proposal_id: str, confirmation_id: str, actor: str) -> SupplierConfirmation:
        """撤回签署：提案回到待确认，修订号 +1，原确认保留为已撤回审计记录。"""
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            if proposal.status is ProposalStatus.APPLIED:
                raise StateError("变更已生效，不可撤回签署")
            confirmation = next(
                (c for c in proposal.confirmations if c.confirmation_id == confirmation_id), None
            )
            if confirmation is None:
                raise NotFoundError(f"确认不存在：{confirmation_id}")
            if confirmation.status is ConfirmationStatus.WITHDRAWN:
                raise StateError("该确认已撤回")
            confirmation.status = ConfirmationStatus.WITHDRAWN
            confirmation.withdrawn_by = actor
            confirmation.withdrawn_at = utcnow()
            proposal.status = ProposalStatus.PENDING
            for item in proposal.items:
                item.decision = ItemDecision.PENDING
                item.responsibility = None
                item.claim_amount = Decimal("0")
                item.assessment_reason = ""
            proposal.revision += 1  # 撤回后旧修订号失效，防止并发确认覆盖
            return confirmation

    def apply_proposal(self, proposal_id: str, expected_order_version: int, actor: str) -> OrderVersion:
        """变更生效：只把已接受的变更项写入新版本（乐观锁：订单版本号）。"""
        with self.store.lock:
            proposal = self._proposal(proposal_id)
            if proposal.status not in (ProposalStatus.ACCEPTED, ProposalStatus.PARTIALLY_ACCEPTED):
                raise StateError(f"当前状态不可生效：{proposal.status.value}")
            chain = self._chain(proposal.order_id)
            latest = chain[-1]
            if latest.version_no != expected_order_version:
                raise ConcurrencyError(
                    f"订单版本冲突：期望 v{expected_order_version}，当前 v{latest.version_no}"
                )
            if proposal.base_version_no != latest.version_no:
                raise ConcurrencyError(
                    f"提案基线 v{proposal.base_version_no} 已过期（当前 v{latest.version_no}），请重新提案"
                )
            base = self._version(chain, proposal.base_version_no)
            updates_by_line: dict[str, dict[LineField, str]] = {}
            for item in proposal.items:
                if item.decision is ItemDecision.ACCEPTED:
                    updates_by_line.setdefault(item.line_id, {})[item.field] = item.new_value
            new_lines = []
            for line in base.lines:
                updates = updates_by_line.get(line.line_id)
                if not updates:
                    new_lines.append(line)
                    continue
                kwargs = {
                    field.value: (Decimal(value) if field in _DECIMAL_FIELDS else value)
                    for field, value in updates.items()
                }
                new_lines.append(replace(line, **kwargs))
            version = OrderVersion(
                proposal.order_id, latest.version_no + 1, latest.version_no,
                tuple(new_lines), "变更生效", proposal.proposal_id, actor,
            )
            chain.append(version)
            proposal.status = ProposalStatus.APPLIED
            return version

    # ---------- 版本比较 ----------

    def compare_versions(self, order_id: str, from_no: int, to_no: int) -> dict:
        """比较同一订单的任意两个版本。

        返回 entries（版本链上每个已生效变更项及其责任方、索赔金额）
        与 summary（两版本间的净差异汇总）。
        """
        with self.store.lock:
            if from_no == to_no:
                raise ValidationError("比较的两个版本相同")
            chain = self._chain(order_id)
            by_no = {v.version_no: v for v in chain}
            lo, hi = sorted((from_no, to_no))
            earlier = self._version(chain, lo)
            later = self._version(chain, hi)

            # 沿父链从 later 回溯到 earlier，验证两版本在同一链条上
            path: list[OrderVersion] = []
            cursor: Optional[OrderVersion] = later
            while cursor is not None and cursor.version_no != earlier.version_no:
                path.append(cursor)
                cursor = by_no.get(cursor.parent_version_no)
            if cursor is None:
                raise ValidationError(f"v{lo} 与 v{hi} 不在同一版本链上")
            path.reverse()

            entries = []
            for version in path:
                if not version.source_proposal_id:
                    continue
                proposal = self.store.proposals[version.source_proposal_id]
                for item in proposal.items:
                    if item.decision is ItemDecision.ACCEPTED:
                        entries.append({
                            "version_no": version.version_no,
                            "proposal_id": proposal.proposal_id,
                            "item_id": item.item_id,
                            "line_id": item.line_id,
                            "field": item.field.value,
                            "old_value": item.old_value,
                            "new_value": item.new_value,
                            "responsibility": item.responsibility.value if item.responsibility else None,
                            "claim_amount": str(item.claim_amount),
                            "reason": item.assessment_reason,
                        })

            summary = []
            earlier_lines = {line.line_id: line for line in earlier.lines}
            for line in later.lines:
                base_line = earlier_lines.get(line.line_id)
                if base_line is None:
                    continue
                for field in LineField:
                    old, new = base_line.value_of(field), line.value_of(field)
                    if old != new:
                        summary.append({
                            "line_id": line.line_id,
                            "field": field.value,
                            "old_value": old,
                            "new_value": new,
                        })
            return {
                "order_id": order_id,
                "from_version": lo,
                "to_version": hi,
                "entries": entries,
                "summary": summary,
            }

    # ---------- 内部辅助 ----------

    def _chain(self, order_id: str) -> list[OrderVersion]:
        try:
            return self.store.versions[order_id]
        except KeyError:
            raise NotFoundError(f"订单不存在：{order_id}") from None

    @staticmethod
    def _version(chain: list[OrderVersion], version_no: int) -> OrderVersion:
        for version in chain:
            if version.version_no == version_no:
                return version
        raise NotFoundError(f"版本不存在：v{version_no}")

    def _proposal(self, proposal_id: str) -> ChangeProposal:
        try:
            return self.store.proposals[proposal_id]
        except KeyError:
            raise NotFoundError(f"提案不存在：{proposal_id}") from None

    @staticmethod
    def _line(version: OrderVersion, line_id: str) -> OrderLine:
        for line in version.lines:
            if line.line_id == line_id:
                return line
        raise ValidationError(f"订单行不存在：{line_id}")

    @staticmethod
    def _check_revision(proposal: ChangeProposal, expected_revision: int) -> None:
        if proposal.revision != expected_revision:
            raise ConcurrencyError(
                f"提案修订号冲突：期望 {expected_revision}，当前 {proposal.revision}"
            )

    @staticmethod
    def _decimal(value, label: str) -> Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation:
            raise ValidationError(f"{label}格式非法：{value!r}") from None

    def _parse_line(self, raw) -> OrderLine:
        if isinstance(raw, OrderLine):
            return raw
        quantity = self._decimal(raw["quantity"], "数量")
        unit_price = self._decimal(raw["unit_price"], "单价")
        if quantity <= 0:
            raise ValidationError("数量必须为正")
        if unit_price < 0:
            raise ValidationError("单价不能为负")
        delivery_date = str(raw["delivery_date"])
        try:
            date.fromisoformat(delivery_date)
        except ValueError:
            raise ValidationError(f"交期必须是 ISO 日期：{delivery_date!r}") from None
        return OrderLine(
            str(raw["line_id"]), str(raw["sku"]), quantity, unit_price,
            delivery_date, str(raw["delivery_location"]),
        )

    @staticmethod
    def _build_items(base: OrderVersion, changes, proposal_id: str) -> list[ChangeItem]:
        if not changes:
            raise ValidationError("提案至少包含一个变更项")
        lines = {line.line_id: line for line in base.lines}
        seen: set[tuple[str, LineField]] = set()
        items = []
        for index, raw in enumerate(changes, 1):
            line_id = str(raw["line_id"])
            field = _parse_enum(LineField, raw["field"], "变更字段")
            if line_id not in lines:
                raise ValidationError(f"订单行不存在：{line_id}")
            key = (line_id, field)
            if key in seen:
                raise ValidationError(f"同一提案内重复变更：{line_id}/{field.value}")
            seen.add(key)
            old_value = lines[line_id].value_of(field)
            new_value = _normalize_value(field, raw["new_value"])
            if new_value == old_value:
                raise ValidationError(f"变更前后值相同：{line_id}/{field.value}")
            items.append(ChangeItem(f"{proposal_id}-I{index}", line_id, field, old_value, new_value))
        return items
