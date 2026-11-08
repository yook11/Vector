"""エージェントのAI呼び出しの失敗が、呼び直せば解消しうるかを起きたことの種類から返す。"""

from __future__ import annotations

from app.agent.output import AgentOutputInvalidError
from app.ai_providers.errors import (
    AIProviderError,
    AIProviderNotSentError,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultError,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.http import retry_prospect as http_retry_prospect


def is_resolvable_by_retry(error: AIProviderError | AgentOutputInvalidError) -> bool:
    """同じ入力で呼び直すだけで解消しうる失敗かを返す。"""
    match error:
        # モデルの生成は呼ぶたびに変わるので、呼び直せば契約を満たす応答が返りうる。
        case AgentOutputInvalidError():
            return True
        case AIProviderTransportError():
            return http_retry_prospect.is_resolvable_by_retry(error.http_error.failure)
        # 設定・権限・日あたりの利用枠・入力や出力の内容は、直すまで同じ結果になる。
        case (
            AIProviderNotSentError()
            | AIProviderResponseError(
                reason=AIProviderResponseReason.AUTH
                | AIProviderResponseReason.LEAKED_API_KEY
                | AIProviderResponseReason.PERMISSION_DENIED
                | AIProviderResponseReason.NOT_FOUND
                | AIProviderResponseReason.FAILED_PRECONDITION
                | AIProviderResponseReason.INSUFFICIENT_BALANCE
                | AIProviderResponseReason.INVALID_REQUEST
                | AIProviderResponseReason.QUOTA_EXHAUSTED
                | AIProviderResponseReason.INPUT_TOO_LONG
                | AIProviderResponseReason.INPUT_BLOCKED
            )
            | AIProviderResultError(
                reason=AIProviderResultReason.INPUT_BLOCKED
                | AIProviderResultReason.OUTPUT_BLOCKED_SAFETY
                | AIProviderResultReason.OUTPUT_BLOCKED_RECITATION
                | AIProviderResultReason.OUTPUT_BLOCKED_BLOCKLIST
                | AIProviderResultReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT
                | AIProviderResultReason.OUTPUT_BLOCKED_SPII
            )
        ):
            return False
        case _:
            return True
