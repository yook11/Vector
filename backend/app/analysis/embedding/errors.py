"""Stage 5 embedding の marker error と provider error adapter。"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire_exceptions import VectorDomainError

# ---------------------------------------------------------------------------
# Layer 1 marker (Stage 5 task 層の dispatch 軸)
# ---------------------------------------------------------------------------


class EmbeddingError(VectorDomainError):
    """Stage 5 全例外の共通基底。直接の catch 対象にはしない。"""

    STAGE: ClassVar[Stage] = Stage.EMBEDDING


class EmbeddingRecoverableError(EmbeddingError):
    """再実行で回復しうる embedding 失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "recoverable"
    RETRYABILITY: ClassVar[Retryability] = Retryability.RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.provider_error = provider_error


class EmbeddingTerminalError(EmbeddingError):
    """再試行は無効で embedding を作らない Stage 5 失敗の共通基底。

    leaf class は audit projection 用の ``FAILURE_KIND`` を必ず宣言する。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    provider_error: AIProviderError | None

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "FAILURE_KIND" not in cls.__dict__:
            raise TypeError(f"{cls.__qualname__} must declare FAILURE_KIND")

    def __init__(
        self,
        *,
        code: str,
        provider_error: AIProviderError | None = None,
    ) -> None:
        if type(self) is EmbeddingTerminalError:
            raise TypeError("EmbeddingTerminalError is abstract")
        super().__init__()
        self.code = code
        self.provider_error = provider_error


class EmbeddingTerminalStageBlockedError(EmbeddingTerminalError):
    """stage/provider 全体が停止している Stage 5 失敗。"""

    FAILURE_KIND: ClassVar[str] = "terminal_stage_blocked"


class EmbeddingTerminalTargetRejectedError(EmbeddingTerminalError):
    """処理対象 assessment 固有の拒否により embedding を作らない失敗。"""

    FAILURE_KIND: ClassVar[str] = "terminal_target_rejected"


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 5 工程由来)
# ---------------------------------------------------------------------------


class EmbeddingResponseInvalidError(EmbeddingRecoverableError):
    """embedder 応答が embedding schema に合致しない。"""

    def __init__(self) -> None:
        super().__init__(
            code="embedding_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``EmbeddingService.execute()`` の boundary で ``to_embedding_error`` を
# 呼ぶ。Stage 5 が「どの provider error を recoverable として扱うか / stage-wide
# terminal と target-local terminal のどちらとして扱うか」を tuple 3 つに集約する
# (Stage 4 ``map_provider_to_assessment`` と完全同形)。
#
# 新しい provider error class が追加されたら、下記の該当 tuple に 1 行追加する
# だけで Stage 5 の解釈に組み込める (コード分岐の追加は不要)。未登録の
# ``AIProviderError`` subclass で ``to_embedding_error`` を呼ぶと
# ``TypeError`` で fail-fast する。


EMBEDDING_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
"""``EmbeddingRecoverableError`` に詰め替えるべき provider error 一覧。

将来の再実行で成功する可能性があるもの (transient / rate limit / usage limit)。
新しい provider error 種別を追加したら必ず本 tuple または下記 terminal tuple
のいずれかに 1 行加える運用ルール。
"""


EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
)
"""``EmbeddingTerminalStageBlockedError`` に詰め替える provider error 一覧。

どの assessment を投入しても同じ失敗になる stage/provider 全体の健全性問題。
観測時に embedding hold を立てる対象。
"""


EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS: tuple[
    type[AIProviderError], ...
] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
"""``EmbeddingTerminalTargetRejectedError`` に詰め替える provider error 一覧。

対象 assessment 固有の content/safety 拒否。stage 全体は健全なため hold しない。
"""


def to_embedding_error(exc: AIProviderError) -> EmbeddingError:
    """provider 例外を Stage 5 marker に詰め替える。"""
    if isinstance(exc, EMBEDDING_RECOVERABLE_PROVIDER_ERRORS):
        return EmbeddingRecoverableError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, EMBEDDING_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS):
        return EmbeddingTerminalStageBlockedError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, EMBEDDING_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS):
        return EmbeddingTerminalTargetRejectedError(
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
