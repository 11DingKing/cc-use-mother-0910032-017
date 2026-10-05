"""采购订单变更控制服务层测试。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from po_change_control.errors import ConcurrencyError, NotFoundError, StateError, ValidationError
from po_change_control.service import ChangeControlService

LINES = [
    {
        "line_no": "L1",
        "sku": "SKU-1",
        "quantity": 100,
        "unit_price": 12.5,
        "delivery_date": "2026-11-01",
        "delivery_location": "上海仓",
    },
    {
        "line_no": "L2",
        "sku": "SKU-2",
        "quantity": 50,
        "unit_price": 30.0,
        "delivery_date": "2026-11-05",
        "delivery_location": "上海仓",
    },
]


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ChangeControlService()
        self.order = self.svc.create_order(supplier_id="SUP-1", lines=LINES, created_by="采购计划员")
        self.oid = self.order["order_id"]
        self.svc.release_order(self.oid, released_by="采购计划员")

    # 工具 -------------------------------------------------------------
    def _submit(self, items, proposed_by="采购计划员", reason="客户生产计划调整"):
        return self.svc.submit_proposal(self.oid, items=items, reason=reason, proposed_by=proposed_by)

    def _confirm(self, pid, decisions, signed_by="供应商"):
        proposal = self.svc.get_proposal(pid)
        return self.svc.confirm_proposal(
            pid, decisions=decisions, signed_by=signed_by, expected_lock_version=proposal["lock_version"]
        )

    def _accept_all(self, pid, signed_by="供应商"):
        proposal = self.svc.get_proposal(pid)
        return self._confirm(pid, {item["item_id"]: "accept" for item in proposal["items"]}, signed_by)

    def _apply(self, pid):
        proposal = self.svc.get_proposal(pid)
        return self.svc.apply_proposal(
            pid, applied_by="采购计划员", expected_lock_version=proposal["lock_version"]
        )

    def _line(self, line_no="L1"):
        order = self.svc.get_order(self.oid)
        return next(line for line in order["lines"] if line["line_no"] == line_no)

    def _version_kinds(self):
        return [v["kind"] for v in self.svc.list_versions(self.oid)]

    # 基线 -------------------------------------------------------------
    def test_baseline_version_created_on_release(self) -> None:
        versions = self.svc.list_versions(self.oid)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["kind"], "订单基线")
        self.assertEqual(versions[0]["seq"], 0)
        snapshot = self.svc.get_version(self.oid, 0)
        self.assertEqual(snapshot["lines"], LINES)
        self.assertEqual(self.svc.get_order(self.oid)["status"], "已下达")

    def test_release_twice_rejected(self) -> None:
        with self.assertRaises(StateError):
            self.svc.release_order(self.oid, released_by="采购计划员")

    # 全部接受 ---------------------------------------------------------
    def test_full_acceptance_applies_all_items(self) -> None:
        proposal = self._submit(
            [
                {"line_no": "L1", "field": "quantity", "new_value": 60},
                {"line_no": "L1", "field": "delivery_date", "new_value": "2026-11-20"},
                {"line_no": "L2", "field": "delivery_location", "new_value": "苏州仓"},
            ]
        )
        self.assertEqual(proposal["status"], "待确认")
        confirmed = self._accept_all(proposal["proposal_id"])
        self.assertEqual(confirmed["status"], "全部接受")
        applied = self._apply(proposal["proposal_id"])
        self.assertEqual(applied["status"], "已生效")
        self.assertEqual(self._line("L1")["quantity"], 60)
        self.assertEqual(self._line("L1")["delivery_date"], "2026-11-20")
        self.assertEqual(self._line("L2")["delivery_location"], "苏州仓")
        self.assertEqual(self._version_kinds(), ["订单基线", "变更生效"])

    # 部分接受：只调整被批准部分 ----------------------------------------
    def test_partial_acceptance_applies_only_approved_items(self) -> None:
        proposal = self._submit(
            [
                {"line_no": "L1", "field": "quantity", "new_value": 60},
                {"line_no": "L1", "field": "delivery_date", "new_value": "2026-11-20"},
                {"line_no": "L2", "field": "delivery_location", "new_value": "苏州仓"},
            ]
        )
        item_ids = [item["item_id"] for item in proposal["items"]]
        confirmed = self._confirm(
            proposal["proposal_id"],
            {item_ids[0]: "accept", item_ids[1]: "reject", item_ids[2]: "accept"},
        )
        self.assertEqual(confirmed["status"], "部分接受")
        self._apply(proposal["proposal_id"])
        # 被批准的生效，被拒绝的保持基线值
        self.assertEqual(self._line("L1")["quantity"], 60)
        self.assertEqual(self._line("L1")["delivery_date"], "2026-11-01")
        self.assertEqual(self._line("L2")["delivery_location"], "苏州仓")

    # 全部拒绝：版本链留痕，内容不变 ------------------------------------
    def test_full_rejection_keeps_content_and_records_chain(self) -> None:
        proposal = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 60}])
        item_id = proposal["items"][0]["item_id"]
        confirmed = self._confirm(proposal["proposal_id"], {item_id: "reject"})
        self.assertEqual(confirmed["status"], "全部拒绝")
        self.assertEqual(self._version_kinds(), ["订单基线", "变更拒绝"])
        self.assertEqual(self._line("L1")["quantity"], 100)
        with self.assertRaises(StateError):
            self._apply(proposal["proposal_id"])

    # 连续变更 ---------------------------------------------------------
    def test_consecutive_changes_form_version_chain(self) -> None:
        p1 = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 60}])
        self._accept_all(p1["proposal_id"])
        self._apply(p1["proposal_id"])
        p2 = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 40}], reason="二次调整")
        self.assertEqual(p2["base_seq"], 1)
        self._accept_all(p2["proposal_id"])
        self._apply(p2["proposal_id"])
        self.assertEqual(self._version_kinds(), ["订单基线", "变更生效", "变更生效"])
        result = self.svc.diff_versions(self.oid, 0, 2)
        self.assertEqual(result["change_count"], 1)
        change = result["changes"][0]
        self.assertEqual((change["old_value"], change["new_value"]), (100, 40))
        self.assertEqual(change["proposal_id"], p2["proposal_id"])

    # 撤回签署 ---------------------------------------------------------
    def test_withdraw_signature_blocks_item_and_records_chain(self) -> None:
        proposal = self._submit(
            [
                {"line_no": "L1", "field": "quantity", "new_value": 60},
                {"line_no": "L2", "field": "delivery_location", "new_value": "苏州仓"},
            ]
        )
        self._accept_all(proposal["proposal_id"])
        item_ids = [item["item_id"] for item in proposal["items"]]
        lock = self.svc.get_proposal(proposal["proposal_id"])["lock_version"]
        withdrawn = self.svc.withdraw_signature(
            proposal["proposal_id"], item_id=item_ids[1], withdrawn_by="供应商", expected_lock_version=lock
        )
        self.assertEqual(withdrawn["status"], "待确认")
        self.assertEqual(self._version_kinds(), ["订单基线", "签署撤回"])
        # 撤回后该项重新决定为拒绝，随后生效只应用被批准的第一项
        self._confirm(proposal["proposal_id"], {item_ids[1]: "reject"})
        self._apply(proposal["proposal_id"])
        self.assertEqual(self._line("L1")["quantity"], 60)
        self.assertEqual(self._line("L2")["delivery_location"], "上海仓")
        self.assertEqual(self._version_kinds(), ["订单基线", "签署撤回", "变更生效"])

    def test_applied_proposal_cannot_withdraw_signature(self) -> None:
        proposal = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 60}])
        self._accept_all(proposal["proposal_id"])
        self._apply(proposal["proposal_id"])
        item_id = proposal["items"][0]["item_id"]
        lock = self.svc.get_proposal(proposal["proposal_id"])["lock_version"]
        with self.assertRaises(StateError):
            self.svc.withdraw_signature(
                proposal["proposal_id"], item_id=item_id, withdrawn_by="供应商", expected_lock_version=lock
            )

    # 乐观锁 -----------------------------------------------------------
    def test_stale_lock_version_rejected(self) -> None:
        proposal = self._submit(
            [
                {"line_no": "L1", "field": "quantity", "new_value": 60},
                {"line_no": "L2", "field": "quantity", "new_value": 30},
            ]
        )
        item_ids = [item["item_id"] for item in proposal["items"]]
        self._confirm(proposal["proposal_id"], {item_ids[0]: "accept"})
        with self.assertRaises(ConcurrencyError):
            self.svc.confirm_proposal(
                proposal["proposal_id"],
                decisions={item_ids[1]: "accept"},
                signed_by="供应商",
                expected_lock_version=0,  # 已过期
            )

    def test_concurrent_confirm_only_one_wins(self) -> None:
        proposal = self._submit(
            [
                {"line_no": "L1", "field": "quantity", "new_value": 60},
                {"line_no": "L2", "field": "quantity", "new_value": 30},
            ]
        )
        item_ids = [item["item_id"] for item in proposal["items"]]
        results: list[str] = []

        def confirm(item_id: str) -> None:
            try:
                self.svc.confirm_proposal(
                    proposal["proposal_id"],
                    decisions={item_id: "accept"},
                    signed_by="供应商",
                    expected_lock_version=0,
                )
                results.append("ok")
            except ConcurrencyError:
                results.append("conflict")

        threads = [threading.Thread(target=confirm, args=(item_id,)) for item_id in item_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])

    # 过期基线 ---------------------------------------------------------
    def test_apply_with_stale_base_seq_rejected(self) -> None:
        p1 = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 60}])
        p2 = self._submit([{"line_no": "L2", "field": "quantity", "new_value": 30}])
        self._accept_all(p1["proposal_id"])
        self._apply(p1["proposal_id"])
        self._accept_all(p2["proposal_id"])
        with self.assertRaises(ConcurrencyError):
            self._apply(p2["proposal_id"])
        # 新提案也必须基于最新版本
        with self.assertRaises(ConcurrencyError):
            self.svc.submit_proposal(
                self.oid,
                items=[{"line_no": "L2", "field": "quantity", "new_value": 20}],
                reason="基于过期版本",
                proposed_by="采购计划员",
                base_seq=0,
            )

    # 受影响承诺、成本证据与责任方 --------------------------------------
    def test_diff_shows_liability_and_claim_evidence(self) -> None:
        commitment = self.svc.add_commitment(
            self.oid, line_no="L1", kind="已备料", quantity=100, note="已按基线备料", created_by="供应商"
        )
        self.svc.add_evidence(
            commitment["commitment_id"], amount=5000, description="备料成本", doc_ref="INV-001", submitted_by="供应商"
        )
        proposal = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 60}])
        self._accept_all(proposal["proposal_id"])
        self._apply(proposal["proposal_id"])
        result = self.svc.diff_versions(self.oid, 0, 1)
        change = result["changes"][0]
        self.assertEqual(change["responsible_party"], "采购方")
        self.assertEqual(change["claim_against"], "采购方")
        self.assertEqual(change["claim_amount_total"], 5000)
        self.assertEqual(change["affected_commitments"][0]["kind"], "已备料")
        self.assertEqual(change["cost_evidence"][0]["doc_ref"], "INV-001")

    def test_supplier_initiated_change_liability_goes_to_supplier(self) -> None:
        proposal = self._submit(
            [{"line_no": "L1", "field": "delivery_date", "new_value": "2026-12-01"}],
            proposed_by="供应商",
            reason="供应商产能不足要求延期",
        )
        self._accept_all(proposal["proposal_id"], signed_by="采购计划员")
        self._apply(proposal["proposal_id"])
        result = self.svc.diff_versions(self.oid, 0, 1)
        change = result["changes"][0]
        self.assertEqual(change["proposed_by"], "供应商")
        self.assertEqual(change["responsible_party"], "供应商")

    # 任意版本比较 -----------------------------------------------------
    def test_diff_between_any_versions(self) -> None:
        p1 = self._submit([{"line_no": "L1", "field": "quantity", "new_value": 60}])
        self._accept_all(p1["proposal_id"])
        self._apply(p1["proposal_id"])
        p2 = self._submit([{"line_no": "L1", "field": "delivery_location", "new_value": "苏州仓"}])
        self._accept_all(p2["proposal_id"])
        self._apply(p2["proposal_id"])
        p3 = self._submit([{"line_no": "L2", "field": "delivery_date", "new_value": "2026-12-15"}])
        self._accept_all(p3["proposal_id"])
        self._apply(p3["proposal_id"])

        full = self.svc.diff_versions(self.oid, 0, 3)
        self.assertEqual(full["change_count"], 3)
        by_field = {(c["line_no"], c["field"]): c for c in full["changes"]}
        self.assertEqual(by_field[("L1", "quantity")]["proposal_id"], p1["proposal_id"])
        self.assertEqual(by_field[("L1", "delivery_location")]["proposal_id"], p2["proposal_id"])
        self.assertEqual(by_field[("L2", "delivery_date")]["proposal_id"], p3["proposal_id"])

        middle = self.svc.diff_versions(self.oid, 1, 3)
        self.assertEqual(middle["change_count"], 2)
        with self.assertRaises(ValidationError):
            self.svc.diff_versions(self.oid, 2, 2)

    # 其它校验 ---------------------------------------------------------
    def test_unknown_order_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.get_order("PO-9999")

    def test_invalid_proposal_item_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._submit([{"line_no": "L1", "field": "sku", "new_value": "X"}])
        with self.assertRaises(ValidationError):
            self._submit([{"line_no": "L9", "field": "quantity", "new_value": 1}])
        with self.assertRaises(ValidationError):
            self._submit([{"line_no": "L1", "field": "quantity", "new_value": 100}])

    def test_proposal_before_release_rejected(self) -> None:
        svc = ChangeControlService()
        order = svc.create_order(supplier_id="SUP-1", lines=LINES, created_by="采购计划员")
        with self.assertRaises(StateError):
            svc.submit_proposal(
                order["order_id"],
                items=[{"line_no": "L1", "field": "quantity", "new_value": 60}],
                reason="过早变更",
                proposed_by="采购计划员",
            )


if __name__ == "__main__":
    unittest.main()
