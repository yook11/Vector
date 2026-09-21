"""工程に依存しないAIプロバイダー例外の型と原因。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from app.logfire.exceptions import VectorDomainError


class AIProviderError(VectorDomainError):
    """provider 由来エラーの共通祖先。Stage の処理方針は持たない。

    ``__init__`` は引数を受けて捨てる (accept-and-discard)。SDK 生 message を
    渡しても ``__str__`` (= Logfire span attribute) に乗らない PII 境界を保ち、
    ad-hoc subclass の互換も維持する。reason を持つのは下位 2 系統
    (``AIProviderStateError`` / ``AIProviderContentError``)。
    """

    CODE: ClassVar[str]
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__()


class AIProviderStateError(AIProviderError):
    """プロバイダー・環境の状態に起因し、任意のreasonを保持する例外。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)

    reason: StrEnum | None

    def __init__(
        self,
        *args: Any,
        reason: StrEnum | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        if reason is not None and not isinstance(reason, StrEnum):
            # PII 境界: 種別ラベル (StrEnum) 以外を reason に通さない。
            raise TypeError("reason must be a StrEnum member or None")
        super().__init__()
        self.reason = reason


class AIProviderContentError(AIProviderError):
    """入出力の内容に起因し、必須のreasonを保持する例外。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason")

    reason: StrEnum

    def __init__(
        self,
        *,
        reason: StrEnum,
    ) -> None:
        if not isinstance(reason, StrEnum):
            # PII 境界: 自由文字列 (AI 生成値) を reason に通さない。
            raise TypeError("reason must be a StrEnum member")
        super().__init__()
        self.reason = reason


# ---------------------------------------------------------------------------
# Content 起因 (入出力内容の拒否)。
# ---------------------------------------------------------------------------


class AIProviderInputRejectedError(AIProviderContentError):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。``reason`` が具体 (input_blocked
    / context_length / safety 等) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"


class AIProviderOutputBlockedError(AIProviderContentError):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。

    ``reason`` が finish_reason 由来の具体 (safety / recitation / blocklist /
    prohibited_content / spii) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_output_blocked"


# ---------------------------------------------------------------------------
# State 起因: 運用側修正が必要 (記事は健全)。
# ---------------------------------------------------------------------------


class AIProviderConfigurationError(AIProviderStateError):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"


class AIProviderRequestInvalidError(AIProviderStateError):
    """request 構造が provider 仕様に合致しない。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"


class AIProviderInsufficientBalanceError(AIProviderStateError):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"


# ---------------------------------------------------------------------------
# State 起因: 通信・利用枠・出力上限の失敗。
# ---------------------------------------------------------------------------


class AIProviderRateLimitedError(AIProviderStateError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。"""

    CODE: ClassVar[str] = "ai_error_rate_limited"


class AIProviderUsageLimitExhaustedError(AIProviderStateError):
    """provider / account / project / model の利用枠を使い切った。時間経過等で復旧。"""

    CODE: ClassVar[str] = "ai_error_usage_limit_exhausted"


class AIProviderServiceUnavailableError(AIProviderStateError):
    """provider 一時障害 (HTTP 5xx)。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"


class AIProviderNetworkError(AIProviderStateError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。"""

    CODE: ClassVar[str] = "ai_error_network"


class AIProviderOutputTruncatedError(AIProviderStateError):
    """finish_reason が MAX_TOKENS で出力が打ち切られた。書き方次第で収まりうる。"""

    CODE: ClassVar[str] = "ai_error_output_truncated"
