"""线程安全的内存仓储，承载订单版本链与变更过程数据。"""
from __future__ import annotations

import itertools
import threading

from .models import ChangeProposal, Commitment, CostEvidence, OrderVersion


class InMemoryStore:
    """按订单聚合的内存存储；所有读写需在 ``lock`` 保护下进行。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.versions: dict[str, list[OrderVersion]] = {}
        self.proposals: dict[str, ChangeProposal] = {}
        self.commitments: dict[str, list[Commitment]] = {}
        self.evidences: dict[str, list[CostEvidence]] = {}
        self._seq = itertools.count(1)

    def next_id(self, prefix: str) -> str:
        """生成单调递增的业务编号。"""
        return f"{prefix}-{next(self._seq):04d}"
