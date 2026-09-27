"""领域异常：HTTP 层按 status 映射为对应的状态码。"""


class DomainError(Exception):
    """领域错误基类。"""

    status = 400


class ValidationError(DomainError):
    """输入不合法（缺字段、数值越界、格式错误等）。"""

    status = 400


class NotFound(DomainError):
    """引用的实体不存在。"""

    status = 404


class StateError(DomainError):
    """当前状态不允许该操作（如未过审核就量产、旧包装超过渡期）。"""

    status = 409
