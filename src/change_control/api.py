"""基于标准库 http.server 的 JSON API。

运行：PYTHONPATH=src python3 -m change_control.api --port 8080

路由：
- POST   /orders                              创建订单基线（v1）
- GET    /orders/{oid}/versions               版本链
- GET    /orders/{oid}/versions/{no}          单个版本
- GET    /orders/{oid}/proposals              订单全部提案
- POST   /orders/{oid}/proposals              发起变更提案
- POST   /orders/{oid}/commitments            登记受影响承诺
- POST   /orders/{oid}/evidences              登记成本证据
- GET    /orders/{oid}/compare?from=&to=      比较任意两个版本（含责任方）
- GET    /proposals/{pid}                     提案详情
- POST   /proposals/{pid}/amend               修订提案（乐观锁）
- POST   /proposals/{pid}/confirm             供应商签署确认（乐观锁）
- POST   /proposals/{pid}/withdraw            撤回签署
- POST   /proposals/{pid}/apply               变更生效（乐观锁）
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ConcurrencyError, DomainError, NotFoundError, StateError, ValidationError
from .service import ChangeControlService


def to_jsonable(value):
    """把领域对象递归转换为 JSON 可序列化结构。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    return value


def _routes(svc: ChangeControlService):
    def actor(body):
        return body.get("actor", "api")

    return [
        ("POST", r"^/orders$",
         lambda q, b: (201, svc.create_order_baseline(b["order_id"], b["lines"], actor(b)))),
        ("GET", r"^/orders/(?P<oid>[^/]+)/versions$",
         lambda q, b, oid: svc.get_version_chain(oid)),
        ("GET", r"^/orders/(?P<oid>[^/]+)/versions/(?P<no>\d+)$",
         lambda q, b, oid, no: svc.get_version(oid, int(no))),
        ("GET", r"^/orders/(?P<oid>[^/]+)/proposals$",
         lambda q, b, oid: svc.list_proposals(oid)),
        ("POST", r"^/orders/(?P<oid>[^/]+)/proposals$",
         lambda q, b, oid: (201, svc.create_proposal(oid, b["initiator"], b["changes"], actor(b)))),
        ("POST", r"^/orders/(?P<oid>[^/]+)/commitments$",
         lambda q, b, oid: (201, svc.register_commitment(
             oid, b["line_id"], b["kind"], b["quantity"], b["unit_cost"], actor(b), b.get("note", "")))),
        ("POST", r"^/orders/(?P<oid>[^/]+)/evidences$",
         lambda q, b, oid: (201, svc.attach_cost_evidence(
             oid, b.get("line_id"), b["amount"], b["description"],
             b.get("attachment_uri", ""), actor(b), b.get("currency", "CNY")))),
        ("GET", r"^/orders/(?P<oid>[^/]+)/compare$",
         lambda q, b, oid: svc.compare_versions(oid, int(q["from"]), int(q["to"]))),
        ("GET", r"^/proposals/(?P<pid>[^/]+)$",
         lambda q, b, pid: svc.get_proposal(pid)),
        ("POST", r"^/proposals/(?P<pid>[^/]+)/amend$",
         lambda q, b, pid: svc.amend_proposal(pid, b["changes"], int(b["expected_revision"]), actor(b))),
        ("POST", r"^/proposals/(?P<pid>[^/]+)/confirm$",
         lambda q, b, pid: (201, svc.confirm_proposal(
             pid, b["decisions"], int(b["expected_revision"]), actor(b)))),
        ("POST", r"^/proposals/(?P<pid>[^/]+)/withdraw$",
         lambda q, b, pid: svc.withdraw_confirmation(pid, b["confirmation_id"], actor(b))),
        ("POST", r"^/proposals/(?P<pid>[^/]+)/apply$",
         lambda q, b, pid: (201, svc.apply_proposal(pid, int(b["expected_order_version"]), actor(b)))),
    ]


class _Handler(BaseHTTPRequestHandler):
    routes: list = []

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, *args) -> None:
        pass  # 保持静默，调用方可自行加日志

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    return self._send(400, {"error": "请求体不是合法 JSON"})
        for route_method, pattern, func in self.routes:
            if route_method != method:
                continue
            match = re.match(pattern, parsed.path)
            if not match:
                continue
            try:
                result = func(query, body, **match.groupdict())
                status, payload = result if isinstance(result, tuple) else (200, result)
                return self._send(status, payload)
            except NotFoundError as e:
                return self._send(404, {"error": str(e)})
            except ConcurrencyError as e:
                return self._send(409, {"error": str(e)})
            except (StateError, ValidationError) as e:
                return self._send(400, {"error": str(e)})
            except DomainError as e:
                return self._send(400, {"error": str(e)})
            except KeyError as e:
                return self._send(400, {"error": f"缺少参数：{e}"})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": f"参数格式非法：{e}"})
        return self._send(404, {"error": "路由不存在"})

    def _send(self, status: int, payload) -> None:
        data = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(address, service: ChangeControlService | None = None) -> ThreadingHTTPServer:
    """构建 HTTP 服务；address 为 (host, port)，端口传 0 表示随机分配。"""
    svc = service or ChangeControlService()
    handler = type("ChangeControlHandler", (_Handler,), {"routes": _routes(svc)})
    return ThreadingHTTPServer(address, handler)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="采购订单变更控制 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = make_server((args.host, args.port))
    print(f"监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
