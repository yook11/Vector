"""工程に依存しないAIプロバイダー例外の型と原因。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class AIProviderError(Exception):
    """通常の例外引数と任意の理由を保持するプロバイダー例外。"""

    CODE: ClassVar[str]
    reason: StrEnum | None

    def __init__(self, *args: object, reason: StrEnum | None = None) -> None:
        if reason is not None and not isinstance(reason, StrEnum):
            raise TypeError("reason must be a StrEnum member or None")
        super().__init__(*args)
        self.reason = reason


class AIProviderInputRejectedError(AIProviderError):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。``reason`` が具体 (input_blocked
    / context_length / safety 等) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"


class AIProviderOutputBlockedError(AIProviderError):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。

    ``reason`` が finish_reason 由来の具体 (safety / recitation / blocklist /
    prohibited_content / spii) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_output_blocked"


class AIProviderConfigurationError(AIProviderError):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"


class AIProviderRequestInvalidError(AIProviderError):
    """request 構造が provider 仕様に合致しない。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"


class AIProviderInsufficientBalanceError(AIProviderError):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"


class AIProviderRateLimitedError(AIProviderError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。"""

    CODE: ClassVar[str] = "ai_error_rate_limited"


class AIProviderUsageLimitExhaustedError(AIProviderError):
    """provider / account / project / model の利用枠を使い切った。時間経過等で復旧。"""

    CODE: ClassVar[str] = "ai_error_usage_limit_exhausted"


class AIProviderServiceUnavailableError(AIProviderError):
    """provider 一時障害 (HTTP 5xx)。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"


class AIProviderNetworkError(AIProviderError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。"""

    CODE: ClassVar[str] = "ai_error_network"


class AIProviderOutputTruncatedError(AIProviderError):
    """finish_reason が MAX_TOKENS で出力が打ち切られた。書き方次第で収まりうる。"""

    CODE: ClassVar[str] = "ai_error_output_truncated"


# 基底型と未知の直接サブクラスは分類済みの失敗として扱わない。
CLASSIFIED_AI_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
    AIProviderServiceUnavailableError,
    AIProviderNetworkError,
    AIProviderOutputTruncatedError,
)
