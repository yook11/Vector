"""AIの失敗を再配信しても変わらないかの判断 (stage 中立)。

分析の3工程はどれも個別の失敗の再試行を SQS の再配信に任せるので、判断を共有する。
"""

from __future__ import annotations

from app.ai_providers.errors import (
    AIProviderError,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
)


def is_unrecoverable_for_input(provider_error: AIProviderError) -> bool:
    """同じ入力では何度送っても結果が変わらない失敗かを返す。"""
    match provider_error:
        case (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_TOO_LONG
                | AIProviderResponseReason.INPUT_BLOCKED
            )
            | AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
        ):
            return True
        # 入力が原因と断定できない失敗は、誤って捨てないよう再配信に任せる。
        case _:
            return False
