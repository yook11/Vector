"""Stage 4 assessment の marker error と provider error adapter。"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire_exceptions import VectorDomainError

# ---------------------------------------------------------------------------
# Layer 1 marker (Stage 4 task 層の dispatch 軸)
# ---------------------------------------------------------------------------


class AssessmentError(VectorDomainError):
    """Stage 4 全例外の共通基底。直接の catch 対象にはしない。"""

    STAGE: ClassVar[Stage] = Stage.ASSESSMENT


class AssessmentRecoverableError(AssessmentError):
    """再実行で回復しうる assessment 失敗。"""

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


class AssessmentTerminalSkipError(AssessmentError):
    """再試行は無効で assessment を作らない Stage 4 失敗。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    FAILURE_KIND: ClassVar[str] = "terminal_skip"
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


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 4 工程由来)
# ---------------------------------------------------------------------------


class AssessmentResponseInvalidError(AssessmentRecoverableError):
    """AI 応答が assessment schema に合致しない。"""

    def __init__(self) -> None:
        super().__init__(
            code="assessment_response_invalid",
            provider_error=None,
        )


class AssessmentCategoryMissingError(AssessmentTerminalSkipError):
    """AI が catalog に存在しない category slug を返した。"""

    def __init__(self) -> None:
        super().__init__(
            code="assessment_category_missing",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``AssessmentService.execute()`` の boundary で ``map_provider_to_assessment`` を
# 呼ぶ。Stage 4 が「どの provider error を recoverable として扱うか / terminal-skip
# として扱うか」を tuple 2 つに集約する (OpenAI evals の
# ``OPENAI_TIMEOUT_EXCEPTIONS`` 流)。Stage 3 が別の方針を持ちたければ Stage 3 専用の
# tuple を作る (本 PR では Stage 3 経路は touch しない)。
#
# 新しい provider error class が追加されたら、下記の該当 tuple に 1 行追加する
# だけで Stage 4 の解釈に組み込める (コード分岐の追加は不要)。未登録の
# ``AIProviderError`` subclass で ``map_provider_to_assessment`` を呼ぶと
# ``TypeError`` で fail-fast する。


ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderQuotaExhaustedError,
)
"""``AssessmentRecoverableError`` に詰め替えるべき provider error 一覧。

将来の再実行で成功する可能性があるもの (transient / rate limit / quota)。
新しい provider error 種別を追加したら必ず本 tuple または下記 terminal-skip tuple
のいずれかに 1 行加える運用ルール。
"""


ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
"""``AssessmentTerminalSkipError`` に詰め替えるべき provider error 一覧。

retry しても同じ結果になる (configuration / request / balance / safety block)。
curation は保持し、assessment 行は作らず audit を焼いて skip する。
"""


def map_provider_to_assessment(exc: AIProviderError) -> AssessmentError:
    """provider 例外を Stage 4 marker に詰め替える。"""
    if isinstance(exc, ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS):
        return AssessmentRecoverableError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS):
        return AssessmentTerminalSkipError(
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
