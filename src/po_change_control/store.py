"""内存仓储：保存订单、提案、版本链、承诺与成本证据。"""
from __future__ import annotations

import threading

from .models import ChangeProposal, Commitment, CostEvidence, Order, Revision


class Store:
    """简单的进程内存储，接口集中，便于替换为持久化实现。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}
        self.orders: dict[str, Order] = {}
        self.proposals: dict[str, ChangeProposal] = {}
        self.revisions: dict[str, list[Revision]] = {}
        self.commitments: dict[str, Commitment] = {}
        self.evidences: dict[str, CostEvidence] = {}

    def next_id(self, prefix: str) -> str:
        """生成确定性递增编号，如 PO-0001。"""
        with self._lock:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            return f"{prefix}-{self._counters[prefix]:04d}"
