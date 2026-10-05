"""HTTP 接口端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from po_change_control.api import create_server

LINES = [
    {
        "line_no": "L1",
        "sku": "SKU-1",
        "quantity": 100,
        "unit_price": 12.5,
        "delivery_date": "2026-11-01",
        "delivery_location": "上海仓",
    }
]


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def _request(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers={"Content-Type": "application/json"}, method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path, payload):
        return self._request("POST", path, payload)

    def _get(self, path):
        return self._request("GET", path)

    def _create_released_order(self) -> str:
        status, order = self._post("/orders", {"supplier_id": "SUP-1", "lines": LINES, "created_by": "采购计划员"})
        self.assertEqual(status, 201)
        status, _ = self._post(f"/orders/{order['order_id']}/release", {"released_by": "采购计划员"})
        self.assertEqual(status, 200)
        return order["order_id"]

    def test_full_change_flow_over_http(self) -> None:
        oid = self._create_released_order()

        # 受影响承诺与成本证据
        status, commitment = self._post(
            f"/orders/{oid}/commitments",
            {"line_no": "L1", "kind": "已备料", "quantity": 100, "created_by": "供应商"},
        )
        self.assertEqual(status, 201)
        status, evidence = self._post(
            f"/commitments/{commitment['commitment_id']}/evidence",
            {"amount": 5000, "description": "备料成本", "doc_ref": "INV-001"},
        )
        self.assertEqual(status, 201)

        # 变更提案 → 供应商确认 → 生效
        status, proposal = self._post(
            f"/orders/{oid}/proposals",
            {
                "items": [
                    {"line_no": "L1", "field": "quantity", "new_value": 60},
                    {"line_no": "L1", "field": "delivery_location", "new_value": "苏州仓"},
                ],
                "reason": "客户生产计划调整",
                "proposed_by": "采购计划员",
            },
        )
        self.assertEqual(status, 201)
        pid = proposal["proposal_id"]
        item_ids = [item["item_id"] for item in proposal["items"]]
        status, confirmed = self._post(
            f"/proposals/{pid}/confirm",
            {
                "decisions": {item_ids[0]: "accept", item_ids[1]: "reject"},
                "signed_by": "供应商",
                "expected_lock_version": proposal["lock_version"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "部分接受")
        status, applied = self._post(
            f"/proposals/{pid}/apply",
            {"applied_by": "采购计划员", "expected_lock_version": confirmed["lock_version"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(applied["status"], "已生效")

        # 只有被批准的数量生效，地点保持基线
        status, order = self._get(f"/orders/{oid}")
        self.assertEqual(status, 200)
        self.assertEqual(order["lines"][0]["quantity"], 60)
        self.assertEqual(order["lines"][0]["delivery_location"], "上海仓")

        # 版本链与差异责任方
        status, versions = self._get(f"/orders/{oid}/versions")
        self.assertEqual([v["kind"] for v in versions["versions"]], ["订单基线", "变更生效"])
        status, diff = self._get(f"/orders/{oid}/diff?from_seq=0&to_seq=1")
        self.assertEqual(status, 200)
        self.assertEqual(diff["change_count"], 1)
        change = diff["changes"][0]
        self.assertEqual(change["field"], "quantity")
        self.assertEqual(change["responsible_party"], "采购方")
        self.assertEqual(change["claim_amount_total"], 5000)
        self.assertEqual(change["affected_commitments"][0]["kind"], "已备料")

    def test_optimistic_lock_conflict_returns_409(self) -> None:
        oid = self._create_released_order()
        status, proposal = self._post(
            f"/orders/{oid}/proposals",
            {"items": [{"line_no": "L1", "field": "quantity", "new_value": 60}], "proposed_by": "采购计划员"},
        )
        self.assertEqual(status, 201)
        pid = proposal["proposal_id"]
        item_id = proposal["items"][0]["item_id"]
        status, _ = self._post(
            f"/proposals/{pid}/confirm",
            {"decisions": {item_id: "accept"}, "signed_by": "供应商", "expected_lock_version": 0},
        )
        self.assertEqual(status, 200)
        # 携带过期锁版本的并发确认被拒绝
        status, error = self._post(
            f"/proposals/{pid}/confirm",
            {"decisions": {item_id: "reject"}, "signed_by": "供应商", "expected_lock_version": 0},
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["type"], "ConcurrencyError")

    def test_unknown_resources_return_404(self) -> None:
        status, _ = self._get("/orders/PO-9999")
        self.assertEqual(status, 404)
        status, _ = self._get("/no-such-route")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
