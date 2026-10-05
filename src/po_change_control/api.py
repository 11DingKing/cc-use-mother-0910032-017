"""采购订单变更控制的 HTTP JSON 接口（仅标准库实现）。

启动：PYTHONPATH=src python3 -m po_change_control.api --port 8080
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ConcurrencyError, DomainError, NotFoundError, StateError, ValidationError
from .service import ChangeControlService

ERROR_STATUS = (
    (NotFoundError, 404),
    (ConcurrencyError, 409),
    (StateError, 422),
    (ValidationError, 400),
)


# ----------------------------------------------------------------------
# 端点处理函数：接收 (service, body, query, **路径参数)，返回 (状态码, 数据)
# ----------------------------------------------------------------------
def _create_order(service, body, query):
    data = service.create_order(
        supplier_id=body["supplier_id"],
        lines=body["lines"],
        created_by=body.get("created_by", "采购计划员"),
        order_id=body.get("order_id"),
    )
    return 201, data


def _get_order(service, body, query, oid):
    return 200, service.get_order(oid)


def _release_order(service, body, query, oid):
    return 200, service.release_order(oid, released_by=body.get("released_by", "采购计划员"))


def _list_versions(service, body, query, oid):
    return 200, {"order_id": oid, "versions": service.list_versions(oid)}


def _get_version(service, body, query, oid, seq):
    return 200, service.get_version(oid, int(seq))


def _submit_proposal(service, body, query, oid):
    data = service.submit_proposal(
        oid,
        items=body["items"],
        reason=body.get("reason", ""),
        proposed_by=body.get("proposed_by", "采购计划员"),
        base_seq=body.get("base_seq"),
    )
    return 201, data


def _list_proposals(service, body, query, oid):
    return 200, {"order_id": oid, "proposals": service.list_proposals(oid)}


def _get_proposal(service, body, query, pid):
    return 200, service.get_proposal(pid)


def _confirm(service, body, query, pid):
    return 200, service.confirm_proposal(
        pid,
        decisions=body["decisions"],
        signed_by=body["signed_by"],
        expected_lock_version=body["expected_lock_version"],
    )


def _withdraw_signature(service, body, query, pid):
    return 200, service.withdraw_signature(
        pid,
        item_id=body["item_id"],
        withdrawn_by=body["withdrawn_by"],
        expected_lock_version=body["expected_lock_version"],
    )


def _apply(service, body, query, pid):
    return 200, service.apply_proposal(
        pid,
        applied_by=body.get("applied_by", "采购计划员"),
        expected_lock_version=body["expected_lock_version"],
    )


def _add_commitment(service, body, query, oid):
    data = service.add_commitment(
        oid,
        line_no=body["line_no"],
        kind=body["kind"],
        quantity=body["quantity"],
        note=body.get("note", ""),
        created_by=body.get("created_by", "供应商"),
    )
    return 201, data


def _add_evidence(service, body, query, cid):
    data = service.add_evidence(
        cid,
        amount=body["amount"],
        currency=body.get("currency", "CNY"),
        description=body.get("description", ""),
        doc_ref=body.get("doc_ref", ""),
        submitted_by=body.get("submitted_by", "供应商"),
    )
    return 201, data


def _diff(service, body, query, oid):
    return 200, service.diff_versions(oid, int(query["from_seq"]), int(query["to_seq"]))


ROUTES = [
    ("POST", re.compile(r"^/orders$"), _create_order),
    ("GET", re.compile(r"^/orders/(?P<oid>[^/]+)$"), _get_order),
    ("POST", re.compile(r"^/orders/(?P<oid>[^/]+)/release$"), _release_order),
    ("GET", re.compile(r"^/orders/(?P<oid>[^/]+)/versions$"), _list_versions),
    ("GET", re.compile(r"^/orders/(?P<oid>[^/]+)/versions/(?P<seq>\d+)$"), _get_version),
    ("POST", re.compile(r"^/orders/(?P<oid>[^/]+)/proposals$"), _submit_proposal),
    ("GET", re.compile(r"^/orders/(?P<oid>[^/]+)/proposals$"), _list_proposals),
    ("GET", re.compile(r"^/proposals/(?P<pid>[^/]+)$"), _get_proposal),
    ("POST", re.compile(r"^/proposals/(?P<pid>[^/]+)/confirm$"), _confirm),
    ("POST", re.compile(r"^/proposals/(?P<pid>[^/]+)/withdraw-signature$"), _withdraw_signature),
    ("POST", re.compile(r"^/proposals/(?P<pid>[^/]+)/apply$"), _apply),
    ("POST", re.compile(r"^/orders/(?P<oid>[^/]+)/commitments$"), _add_commitment),
    ("POST", re.compile(r"^/commitments/(?P<cid>[^/]+)/evidence$"), _add_evidence),
    ("GET", re.compile(r"^/orders/(?P<oid>[^/]+)/diff$"), _diff),
]


def create_server(host: str = "127.0.0.1", port: int = 8080, service: ChangeControlService | None = None):
    """创建 HTTP 服务，service 可注入以便测试。"""
    svc = service or ChangeControlService()

    class Handler(BaseHTTPRequestHandler):
        server_version = "POChangeControl/1.0"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *args):  # 静默访问日志
            pass

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except json.JSONDecodeError:
                        self._send(400, {"error": "请求体不是合法 JSON"})
                        return
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            for route_method, pattern, func in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if not match:
                    continue
                try:
                    status, payload = func(svc, body, query, **match.groupdict())
                except DomainError as exc:
                    status = next((code for cls, code in ERROR_STATUS if isinstance(exc, cls)), 400)
                    self._send(status, {"error": str(exc), "type": type(exc).__name__})
                except (KeyError, ValueError) as exc:
                    self._send(400, {"error": f"请求参数缺失或不合法：{exc}"})
                else:
                    self._send(status, payload)
                return
            self._send(404, {"error": "接口不存在"})

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="采购订单变更控制服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = create_server(host=args.host, port=args.port)
    print(f"采购订单变更控制服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
