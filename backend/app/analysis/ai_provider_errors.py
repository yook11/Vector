"""Stage 中立な AI provider origin error。"""

from __future__ import annotations

from typing import Any, ClassVar

from app.logfire_exceptions import VectorDomainError


class AIProviderError(VectorDomainError):
    """provider 由来エラーの共通祖先。Stage の処理方針は持たない。"""

    CODE: ClassVar[str]
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__()


# ---------------------------------------------------------------------------
# provider が明示的に処理拒否したケース (Stage 3 では DROP_ARTICLE 行き)
# ---------------------------------------------------------------------------


class AIProviderInputRejectedError(AIProviderError):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"


class AIProviderOutputBlockedError(AIProviderError):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。"""

    CODE: ClassVar[str] = "ai_error_output_blocked"


# ---------------------------------------------------------------------------
# 運用側修正が必要 (記事は健全、Stage 3 では KEEP_ARTICLE 行き)
# ---------------------------------------------------------------------------


class AIProviderConfigurationError(AIProviderError):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"


class AIProviderRequestInvalidError(AIProviderError):
    """request 構造が provider 仕様に合致しない。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"


class AIProviderInsufficientBalanceError(AIProviderError):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"


# ---------------------------------------------------------------------------
# 一時障害 (Stage 3 では RETRYABLE 行き)
# ---------------------------------------------------------------------------


class AIProviderRateLimitedError(AIProviderError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。"""

    CODE: ClassVar[str] = "ai_error_rate_limited"


class AIProviderQuotaExhaustedError(AIProviderError):
    """日次 quota (RPD) 到達。翌日まで recover 見込みなしだが再 dispatch 可能。"""

    CODE: ClassVar[str] = "ai_error_quota_exhausted"


class AIProviderServiceUnavailableError(AIProviderError):
    """provider 一時障害 (HTTP 5xx)。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"


class AIProviderNetworkError(AIProviderError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。"""

    CODE: ClassVar[str] = "ai_error_network"
