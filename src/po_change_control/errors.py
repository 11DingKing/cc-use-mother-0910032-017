"""领域异常定义。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""


class NotFoundError(DomainError):
    """资源不存在。"""


class ValidationError(DomainError):
    """输入不合法。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class ConcurrencyError(DomainError):
    """乐观锁冲突：数据已被他人修改。"""
