"""回答工程が再試行の判断に使う、失敗の見分け方。"""

from __future__ import annotations

from typing import TypeGuard

from app.ai_providers.errors import (
    AIProviderResultError,
    AIProviderResultReason,
)


def is_output_truncated(exc: BaseException) -> TypeGuard[AIProviderResultError]:
    """出力が上限で打ち切られた失敗か。"""
    return (
        isinstance(exc, AIProviderResultError)
        and exc.reason is AIProviderResultReason.OUTPUT_TRUNCATED
    )
