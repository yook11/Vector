"""工程に依存しないAIプロバイダー例外の型と原因。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.shared.errors import ApplicationError, ApplicationErrorValue


class AIProviderError(ApplicationError):
    """入力値を含めない説明と分類済みの診断を保持するプロバイダー例外。"""

    CODE: ClassVar[str]
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの処理に失敗しました"
    reason: StrEnum | None

    def __init__(
        self, message: str | None = None, *, reason: StrEnum | None = None
    ) -> None:
        if message is not None and not isinstance(message, str):
            raise TypeError("message must be a string or None")
        if reason is not None and not isinstance(reason, StrEnum):
            raise TypeError("reason must be a StrEnum member or None")
        details: dict[str, ApplicationErrorValue] = {}
        code = getattr(type(self), "CODE", None)
        if code is not None:
            details["code"] = code
        if reason is not None:
            details["reason"] = reason.value
        super().__init__(
            self.DEFAULT_MESSAGE if message is None else message,
            details=details or None,
        )
        self.reason = reason


class AIProviderInputRejectedError(AIProviderError):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。``reason`` が具体 (input_blocked
    / context_length / safety 等) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーが入力を拒否しました"


class AIProviderOutputBlockedError(AIProviderError):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。

    ``reason`` が finish_reason 由来の具体 (safety / recitation / blocklist /
    prohibited_content / spii) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_output_blocked"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーが応答の出力を抑止しました"


class AIProviderConfigurationError(AIProviderError):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの設定または利用条件が不正です"


class AIProviderRequestInvalidError(AIProviderError):
    """request 構造が provider 仕様に合致しない。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーへのリクエストが不正です"


class AIProviderInsufficientBalanceError(AIProviderError):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの利用残高が不足しています"


class AIProviderRateLimitedError(AIProviderError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。"""

    CODE: ClassVar[str] = "ai_error_rate_limited"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの呼び出し頻度の上限に達しました"


class AIProviderUsageLimitExhaustedError(AIProviderError):
    """provider / account / project / model の利用枠を使い切った。時間経過等で復旧。"""

    CODE: ClassVar[str] = "ai_error_usage_limit_exhausted"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの利用枠を使い切りました"


class AIProviderServiceUnavailableError(AIProviderError):
    """provider 一時障害 (HTTP 5xx)。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーのサービスを利用できません"


class AIProviderNetworkError(AIProviderError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。"""

    CODE: ClassVar[str] = "ai_error_network"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーとの通信に失敗しました"


class AIProviderOutputTruncatedError(AIProviderError):
    """finish_reason が MAX_TOKENS で出力が打ち切られた。書き方次第で収まりうる。"""

    CODE: ClassVar[str] = "ai_error_output_truncated"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの応答が途中で打ち切られました"


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
