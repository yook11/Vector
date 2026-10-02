"""AIの失敗を再配信せずに受信完了にするかの判断 (stage 中立)。

分析の3工程はどれも個別の失敗の再試行を SQS の再配信に任せるので、判断を共有する。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai_providers.errors import (
    AIProviderError,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
)


@dataclass(frozen=True, slots=True)
class SettledProviderFailure:
    """この入力では回復しないため、再配信せずに受信完了にするAIの失敗。"""

    provider_error: AIProviderError


def settled_provider_failure(
    provider_error: AIProviderError,
) -> SettledProviderFailure | None:
    """同じ入力では結果が変わらない失敗だけを受信完了にし、他は再配信に任せる。"""
    match provider_error:
        case (
            AIProviderResponseError(
                reason=AIProviderResponseReason.INPUT_TOO_LONG
                | AIProviderResponseReason.INPUT_BLOCKED
            )
            | AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
        ):
            return SettledProviderFailure(provider_error)
        # 入力が原因と断定できない失敗は、誤って捨てないよう再配信に任せる。
        case _:
            return None
