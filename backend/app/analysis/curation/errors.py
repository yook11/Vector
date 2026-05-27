"""Stage 3 curation の marker error と provider error adapter。"""

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
# Layer 1 marker (Stage 3 task 層の dispatch 軸、3 axis)
# ---------------------------------------------------------------------------


class CurationError(VectorDomainError):
    """Stage 3 全例外の共通基底。直接の catch 対象にはしない。"""

    STAGE: ClassVar[Stage] = Stage.CURATION


class CurationRecoverableError(CurationError):
    """再実行で回復しうる curation 失敗。"""

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


class CurationTerminalKeepError(CurationError):
    """再試行は無効だが article は保持する curation 失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "terminal_keep"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
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


class CurationTerminalDropError(CurationError):
    """再試行は無効で article 削除を伴う curation 失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "terminal_drop"
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = FailureAction.DROP_ARTICLE

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


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 3 工程由来)
# ---------------------------------------------------------------------------


class CurationResponseInvalidError(CurationRecoverableError):
    """AI 応答が curation schema に合致しない。"""

    def __init__(self) -> None:
        super().__init__(
            code="extraction_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``CurationService.execute()`` の boundary で ``map_provider_to_curation`` を
# 呼ぶ。Stage 3 は article DELETE / Keep / Recoverable の 3 軸を持つので tuple も
# 3 つに分かれる。Stage 4/5 とは tuple 数のみ異なり、構造は同じ。
#
# 新しい provider error class が追加されたら、下記の該当 tuple に 1 行追加する
# だけで Stage 3 の解釈に組み込める (コード分岐の追加は不要)。未登録の
# ``AIProviderError`` subclass で ``map_provider_to_curation`` を呼ぶと
# ``TypeError`` で fail-fast する。


CURATION_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderUsageLimitExhaustedError,
)
"""``CurationRecoverableError`` に詰め替えるべき provider error 一覧。

将来の再実行で成功する可能性があるもの (transient / rate limit / usage limit)。
"""


CURATION_TERMINAL_KEEP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
)
"""``CurationTerminalKeepError`` に詰め替えるべき provider error 一覧。

retry しても同じ結果になるが article 自体は健全 (configuration / request /
balance)。article は保持し audit のみ焼く。
"""


CURATION_TERMINAL_DROP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
"""``CurationTerminalDropError`` に詰め替えるべき provider error 一覧。

provider が記事入力を明示拒否 or 応答を policy 抑制 = 記事自体に問題あり。
article DELETE 対象。
"""


def map_provider_to_curation(exc: AIProviderError) -> CurationError:
    """provider 例外を Stage 3 marker に詰め替える。"""
    if isinstance(exc, CURATION_RECOVERABLE_PROVIDER_ERRORS):
        return CurationRecoverableError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, CURATION_TERMINAL_KEEP_PROVIDER_ERRORS):
        return CurationTerminalKeepError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, CURATION_TERMINAL_DROP_PROVIDER_ERRORS):
        return CurationTerminalDropError(
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
