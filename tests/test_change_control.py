"""采购订单变更控制后端的行为测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from change_control.api import make_server
from change_control.errors import ConcurrencyError, NotFoundError, StateError, ValidationError
from change_control.models import ConfirmationStatus, ItemDecision, ProposalStatus, Responsibility
from change_control.service import ChangeControlService


def _lines():
    return [
        {"line_id": "L1", "sku": "SKU-A", "quantity": "80", "unit_price": "10.00",
         "delivery_date": "2026-11-01", "delivery_location": "上海仓"},
        {"line_id": "L2", "sku": "SKU-B", "quantity": "40", "unit_price": "25.00",
         "delivery_date": "2026-11-05", "delivery_location": "上海仓"},
    ]


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.svc = ChangeControlService()
        self.svc.create_order_baseline("PO-1", _lines(), actor="采购计划员")

    def _propose(self, changes, initiator="BUYER"):
        return self.svc.create_proposal("PO-1", initiator, changes, actor="采购计划员")

    def _accept_all(self, proposal):
        decisions = {item.item_id: "ACCEPT" for item in proposal.items}
        return self.svc.confirm_proposal(
            proposal.proposal_id, decisions, proposal.revision, actor="供应商"
        )

    def test_partial_acceptance_applies_only_approved_items(self):
        self.svc.register_commitment("PO-1", "L1", "MATERIAL_PREPARED", "100", "5.00", actor="供应商")
        self.svc.attach_cost_evidence("PO-1", "L1", "200", "备料损耗单", "oss://evidence/1", actor="供应商")
        proposal = self._propose([
            {"line_id": "L1", "field": "quantity", "new_value": "50"},
            {"line_id": "L1", "field": "delivery_location", "new_value": "广州仓"},
            {"line_id": "L2", "field": "delivery_date", "new_value": "2026-11-20"},
        ])
        decisions = {
            item.item_id: ("REJECT" if item.field.value == "delivery_location" else "ACCEPT")
            for item in proposal.items
        }
        self.svc.confirm_proposal(proposal.proposal_id, decisions, proposal.revision, actor="供应商")
        self.assertEqual(proposal.status, ProposalStatus.PARTIALLY_ACCEPTED)

        v2 = self.svc.apply_proposal(proposal.proposal_id, expected_order_version=1, actor="采购计划员")
        self.assertEqual((v2.version_no, v2.parent_version_no, v2.cause), (2, 1, "变更生效"))
        lines = {line.line_id: line for line in v2.lines}
        self.assertEqual(lines["L1"].quantity, Decimal("50"))
        self.assertEqual(lines["L1"].delivery_location, "上海仓")  # 被拒绝项不生效
        self.assertEqual(lines["L2"].delivery_date, "2026-11-20")

        qty_item = next(i for i in proposal.items if i.field.value == "quantity")
        self.assertEqual(qty_item.responsibility, Responsibility.BUYER)
        # 受保护差额 min(100, 80) - 50 = 30，加权成本 5 → 150，加成本证据 200 → 350
        self.assertEqual(qty_item.claim_amount, Decimal("350.00"))
        self.assertIn("采购方", qty_item.assessment_reason)
        rejected = next(i for i in proposal.items if i.field.value == "delivery_location")
        self.assertIsNone(rejected.responsibility)

    def test_full_rejection_is_terminal(self):
        proposal = self._propose([{"line_id": "L1", "field": "quantity", "new_value": "60"}])
        self.svc.confirm_proposal(
            proposal.proposal_id, {proposal.items[0].item_id: "REJECT"},
            proposal.revision, actor="供应商",
        )
        self.assertEqual(proposal.status, ProposalStatus.REJECTED)
        with self.assertRaises(StateError):
            self.svc.apply_proposal(proposal.proposal_id, expected_order_version=1, actor="采购计划员")

    def test_confirm_requires_current_revision(self):
        proposal = self._propose([{"line_id": "L1", "field": "quantity", "new_value": "60"}])
        self.svc.amend_proposal(
            proposal.proposal_id,
            [{"line_id": "L1", "field": "quantity", "new_value": "55"}],
            expected_revision=1, actor="采购计划员",
        )
        self.assertEqual(proposal.revision, 2)
        with self.assertRaises(ConcurrencyError):  # 供应商按旧修订号确认 → 冲突
            self.svc.confirm_proposal(
                proposal.proposal_id, {proposal.items[0].item_id: "ACCEPT"},
                expected_revision=1, actor="供应商",
            )
        self.svc.confirm_proposal(
            proposal.proposal_id, {proposal.items[0].item_id: "ACCEPT"},
            expected_revision=2, actor="供应商",
        )
        self.assertEqual(proposal.status, ProposalStatus.ACCEPTED)

    def test_concurrent_apply_conflict_and_consecutive_chain(self):
        pa = self._propose([{"line_id": "L1", "field": "quantity", "new_value": "70"}])
        pb = self._propose([{"line_id": "L2", "field": "quantity", "new_value": "45"}])
        self._accept_all(pa)
        self._accept_all(pb)
        self.svc.apply_proposal(pa.proposal_id, expected_order_version=1, actor="采购计划员")
        with self.assertRaises(ConcurrencyError):  # pb 的基线已被 pa 顶掉
            self.svc.apply_proposal(pb.proposal_id, expected_order_version=1, actor="采购计划员")

        # 连续变更：基于最新版本重新提案
        pb2 = self._propose([{"line_id": "L2", "field": "quantity", "new_value": "45"}])
        self.assertEqual(pb2.base_version_no, 2)
        self._accept_all(pb2)
        v3 = self.svc.apply_proposal(pb2.proposal_id, expected_order_version=2, actor="采购计划员")

        chain = self.svc.get_version_chain("PO-1")
        self.assertEqual([v.version_no for v in chain], [1, 2, 3])
        self.assertEqual([v.parent_version_no for v in chain], [None, 1, 2])
        self.assertEqual(chain[1].source_proposal_id, pa.proposal_id)
        self.assertEqual(chain[2].source_proposal_id, pb2.proposal_id)
        lines = {line.line_id: line for line in v3.lines}
        self.assertEqual(lines["L1"].quantity, Decimal("70"))
        self.assertEqual(lines["L2"].quantity, Decimal("45"))

    def test_withdraw_confirmation_returns_to_pending(self):
        proposal = self._propose([{"line_id": "L1", "field": "quantity", "new_value": "60"}])
        confirmation = self._accept_all(proposal)
        self.assertEqual(proposal.status, ProposalStatus.ACCEPTED)

        self.svc.withdraw_confirmation(proposal.proposal_id, confirmation.confirmation_id, actor="供应商")
        self.assertEqual(confirmation.status, ConfirmationStatus.WITHDRAWN)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        self.assertEqual(proposal.revision, 2)
        self.assertEqual(proposal.items[0].decision, ItemDecision.PENDING)
        self.assertIsNone(proposal.items[0].responsibility)

        with self.assertRaises(ConcurrencyError):  # 撤回后旧修订号失效
            self.svc.confirm_proposal(
                proposal.proposal_id, {proposal.items[0].item_id: "ACCEPT"},
                expected_revision=1, actor="供应商",
            )
        self.svc.confirm_proposal(
            proposal.proposal_id, {proposal.items[0].item_id: "ACCEPT"},
            expected_revision=2, actor="供应商",
        )
        self.svc.apply_proposal(proposal.proposal_id, expected_order_version=1, actor="采购计划员")
        self.assertEqual(len(self.svc.get_version_chain("PO-1")), 2)
        self.assertEqual(len(proposal.confirmations), 2)  # 撤回记录保留审计
        with self.assertRaises(StateError):  # 已生效不可再撤回
            self.svc.withdraw_confirmation(
                proposal.proposal_id, proposal.confirmations[1].confirmation_id, actor="供应商"
            )

    def test_supplier_initiated_change_bears_supplier_responsibility(self):
        self.svc.attach_cost_evidence("PO-1", "L2", "500", "加急改期损失", "oss://evidence/2", actor="采购计划员")
        proposal = self._propose(
            [{"line_id": "L2", "field": "delivery_date", "new_value": "2026-12-01"}],
            initiator="SUPPLIER",
        )
        self._accept_all(proposal)
        item = proposal.items[0]
        self.assertEqual(item.responsibility, Responsibility.SUPPLIER)
        self.assertEqual(item.claim_amount, Decimal("500"))

    def test_buyer_change_without_commitment_is_liability_free(self):
        proposal = self._propose([{"line_id": "L1", "field": "quantity", "new_value": "60"}])
        self._accept_all(proposal)
        self.assertEqual(proposal.items[0].responsibility, Responsibility.NONE)
        self.assertEqual(proposal.items[0].claim_amount, Decimal("0"))

    def test_compare_versions_reports_responsibility_per_item(self):
        self.svc.register_commitment("PO-1", "L1", "MATERIAL_PREPARED", "80", "5.00", actor="供应商")
        p1 = self._propose([{"line_id": "L1", "field": "quantity", "new_value": "60"}])
        self._accept_all(p1)
        self.svc.apply_proposal(p1.proposal_id, expected_order_version=1, actor="采购计划员")
        p2 = self._propose([{"line_id": "L2", "field": "delivery_location", "new_value": "深圳仓"}])
        self._accept_all(p2)
        self.svc.apply_proposal(p2.proposal_id, expected_order_version=2, actor="采购计划员")

        report = self.svc.compare_versions("PO-1", 1, 3)
        self.assertEqual((report["from_version"], report["to_version"]), (1, 3))
        self.assertEqual(len(report["entries"]), 2)
        first, second = report["entries"]
        self.assertEqual(first["version_no"], 2)
        self.assertEqual(first["responsibility"], Responsibility.BUYER.value)
        self.assertEqual(first["claim_amount"], "100.00")  # (min(80,80)-60) × 5
        self.assertEqual(second["version_no"], 3)
        self.assertEqual(second["responsibility"], Responsibility.NONE.value)
        self.assertEqual(report["summary"], [
            {"line_id": "L1", "field": "quantity", "old_value": "80", "new_value": "60"},
            {"line_id": "L2", "field": "delivery_location", "old_value": "上海仓", "new_value": "深圳仓"},
        ])

        # 任意区间、反向比较
        self.assertEqual(len(self.svc.compare_versions("PO-1", 2, 3)["entries"]), 1)
        self.assertEqual(len(self.svc.compare_versions("PO-1", 3, 1)["entries"]), 2)
        with self.assertRaises(ValidationError):
            self.svc.compare_versions("PO-1", 2, 2)

    def test_validation_and_not_found(self):
        with self.assertRaises(NotFoundError):
            self.svc.get_version_chain("PO-X")
        with self.assertRaises(ValidationError):  # 重复订单
            self.svc.create_order_baseline("PO-1", _lines(), actor="采购计划员")
        with self.assertRaises(ValidationError):  # 同一提案重复变更同一字段
            self._propose([
                {"line_id": "L1", "field": "quantity", "new_value": "60"},
                {"line_id": "L1", "field": "quantity", "new_value": "61"},
            ])
        with self.assertRaises(ValidationError):  # 变更前后值相同
            self._propose([{"line_id": "L1", "field": "quantity", "new_value": "80"}])
        with self.assertRaises(ValidationError):  # 未知订单行
            self._propose([{"line_id": "L9", "field": "quantity", "new_value": "1"}])
        with self.assertRaises(ValidationError):  # 非法交期
            self._propose([{"line_id": "L1", "field": "delivery_date", "new_value": "下周"}])
        with self.assertRaises(ValidationError):  # 确认未覆盖全部变更项
            proposal = self._propose([
                {"line_id": "L1", "field": "quantity", "new_value": "60"},
                {"line_id": "L2", "field": "quantity", "new_value": "41"},
            ])
            self.svc.confirm_proposal(
                proposal.proposal_id, {proposal.items[0].item_id: "ACCEPT"},
                proposal.revision, actor="供应商",
            )


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(("127.0.0.1", 0))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_http_flow_and_error_mapping(self):
        status, order = self._request("POST", "/orders", {
            "order_id": "PO-HTTP", "lines": _lines(), "actor": "采购计划员",
        })
        self.assertEqual((status, order["version_no"]), (201, 1))

        status, _ = self._request("POST", "/orders/PO-HTTP/commitments", {
            "line_id": "L1", "kind": "MATERIAL_PREPARED", "quantity": "80",
            "unit_cost": "5.00", "actor": "供应商",
        })
        self.assertEqual(status, 201)

        status, proposal = self._request("POST", "/orders/PO-HTTP/proposals", {
            "initiator": "BUYER", "actor": "采购计划员",
            "changes": [{"line_id": "L1", "field": "quantity", "new_value": "66"}],
        })
        self.assertEqual(status, 201)
        item_id = proposal["items"][0]["item_id"]

        status, body = self._request("POST", f"/proposals/{proposal['proposal_id']}/confirm", {
            "decisions": {item_id: "ACCEPT"}, "expected_revision": 99, "actor": "供应商",
        })
        self.assertEqual(status, 409)  # 乐观锁冲突
        self.assertIn("修订号冲突", body["error"])

        status, _ = self._request("POST", f"/proposals/{proposal['proposal_id']}/confirm", {
            "decisions": {item_id: "ACCEPT"}, "expected_revision": 1, "actor": "供应商",
        })
        self.assertEqual(status, 201)

        status, version = self._request("POST", f"/proposals/{proposal['proposal_id']}/apply", {
            "expected_order_version": 1, "actor": "采购计划员",
        })
        self.assertEqual((status, version["version_no"]), (201, 2))

        status, report = self._request("GET", "/orders/PO-HTTP/compare?from=1&to=2")
        self.assertEqual(status, 200)
        self.assertEqual(report["entries"][0]["responsibility"], "采购方")
        self.assertEqual(report["entries"][0]["claim_amount"], "70.00")  # (80-66) × 5

        status, _ = self._request("GET", "/orders/PO-NONE/versions")
        self.assertEqual(status, 404)
        status, _ = self._request("GET", "/orders/PO-HTTP/compare?from=1")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
