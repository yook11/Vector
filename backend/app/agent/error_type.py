"""span の ``error.type`` が名乗る失敗種別。"""

from __future__ import annotations

from app.agent.runtime.contract import AgentResponseInvalidError

__all__ = ["span_error_type"]


def span_error_type(error: BaseException) -> str:
    """工程spanとprovider spanが同じ語彙でerror.typeを名乗るための唯一の写像。"""
    if isinstance(error, AgentResponseInvalidError):
        return error.defect.value
    code = getattr(error, "CODE", None)
    if isinstance(code, str):
        return code
    return type(error).__name__
