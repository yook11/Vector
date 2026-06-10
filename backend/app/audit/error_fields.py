"""監査行の error_class / error_message field を組み立てる pure helper。

``error_chain`` (error_chain.py) と同様、session も I/O も持たない純粋関数で
各 stage の ``audit.stages.*`` から再利用される。
"""

from __future__ import annotations

from app.shared.security.redaction import redact_secrets

_ERROR_MESSAGE_LIMIT = 2000


def exception_fqn(exc: BaseException) -> str:
    """``error_class`` に焼く例外型の FQN。"""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def redacted_audit_message(text: str) -> str | None:
    """secret を mask し監査上限で切り詰める (空は None)。"""
    return redact_secrets(text)[:_ERROR_MESSAGE_LIMIT] or None


def error_message_of(exc: BaseException | None) -> str | None:
    """``error_message`` に焼く例外メッセージ (None 例外は None passthrough)。"""
    if exc is None:
        return None
    return redacted_audit_message(str(exc))
