"""旧Taskiqと再キュレーションCLIの失敗分類を維持する。"""

from __future__ import annotations

from typing import ClassVar

from app.ai_providers.errors import AIProviderError, AIProviderFailureMode
from app.analysis.curation.errors import CurationError, CurationFailureReason
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire.exceptions import VectorDomainError


class CurationTaskError(VectorDomainError):
    """旧経路の再試行・保持・削除に用いる失敗分類。"""


class CurationRecoverableError(CurationTaskError):
    """再実行で回復しうる旧経路の失敗分類。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


class CurationTerminalKeepError(CurationTaskError):
    """再試行は無効だが article は保持する curation 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY`` = NON_RETRYABLE) だけ。stage hold
    を立てるかは型では決めず handler が provider error の ``FAILURE_MODE`` から導出
    する。原因軸 (``failure_kind`` / ``failure_reason``) は instance 値。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


class CurationTerminalDropError(CurationTaskError):
    """再試行は無効で article 削除を伴う curation 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY``) と業務副作用 (``FAILURE_ACTION`` =
    DROP_ARTICLE)。この業務 disposition が 3 本目の marker を正当化する。原因軸
    (``failure_kind`` / ``failure_reason``) は instance 値。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = FailureAction.DROP_ARTICLE

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


def to_curation_task_error(exc: BaseException) -> BaseException:
    """失敗理由を旧経路の方針へ対応付け、元の原因を保持する。"""
    if not isinstance(exc, CurationError):
        return exc
    if exc.reason is CurationFailureReason.PROVIDER_ERROR:
        provider = exc.provider_error
        if provider is None:
            raise TypeError("provider error is required")
        mode = provider.FAILURE_MODE
        marker: type[
            CurationRecoverableError
            | CurationTerminalKeepError
            | CurationTerminalDropError
        ]
        if mode.retryable:
            marker = CurationRecoverableError
        elif mode is AIProviderFailureMode.TARGET_REJECTED:
            marker = CurationTerminalDropError
        else:
            marker = CurationTerminalKeepError
        result = marker(
            code=exc.code,
            failure_kind=mode.value,
            failure_reason=provider.reason.value
            if provider.reason is not None
            else None,
            provider_error=provider,
        )
    else:
        result = CurationRecoverableError(
            code=exc.code, failure_kind="ai_response_invalid"
        )
    result.__cause__ = exc
    return result
