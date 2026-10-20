"""例外型の FQN をログ・span の ``error_class`` へ載せる pure helper。"""

from __future__ import annotations


def exception_fqn(exc: BaseException) -> str:
    """``error_class`` に焼く例外型の FQN。"""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
