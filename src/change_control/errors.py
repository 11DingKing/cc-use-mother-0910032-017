"""领域异常定义。"""


class DomainError(Exception):
    """领域层基础异常。"""


class NotFoundError(DomainError):
    """目标实体不存在。"""


class ValidationError(DomainError):
    """输入或领域规则校验失败。"""


class StateError(DomainError):
    """非法的状态迁移。"""


class ConcurrencyError(DomainError):
    """乐观锁冲突：预期版本与实际版本不一致。"""
