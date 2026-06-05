"""Stage 3 curation の marker error と provider error adapter。"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderError,
    AIProviderFailureMode,
    AIProviderStateError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire.exceptions import VectorDomainError

# ---------------------------------------------------------------------------
# Layer 1 marker (Stage 3 task 層の dispatch 軸、3 axis)
# ---------------------------------------------------------------------------


class CurationError(VectorDomainError):
    """Stage 3 全例外の共通基底。直接の catch 対象にはしない。"""

    STAGE: ClassVar[Stage] = Stage.CURATION


class CurationRecoverableError(CurationError):
    """再実行で回復しうる curation 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY``) だけ。原因軸 (``failure_kind`` =
    回復クラス / ``failure_reason`` = 詳細) は instance 値で持ち、provider error の
    ``FAILURE_MODE`` / ``reason`` から ``map_provider_to_curation`` が導出する。
    ``failure_reason`` は forensic 用で ``SAFE_ATTRS`` に含めない (``str(exc)`` は
    ``(code=...)`` を保つ)。audit へは ``failure_projection`` 経由で焼く。
    """

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


class CurationTerminalKeepError(CurationError):
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


class CurationTerminalDropError(CurationError):
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


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 3 工程由来)
# ---------------------------------------------------------------------------


class CurationResponseInvalidError(CurationRecoverableError):
    """AI 応答が curation schema に合致しない。

    原因ファミリーは provider 起因ではないため ``failure_kind="ai_response_invalid"``
    で固定する。``code`` は既存契約 (``extraction_response_invalid``) を据え置く。
    """

    def __init__(self) -> None:
        super().__init__(
            code="extraction_response_invalid",
            failure_kind="ai_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``CurationService.execute()`` の boundary で ``map_provider_to_curation`` を
# 呼ぶ。retry 軸 (Recoverable / Terminal) と DROP 副作用は provider の回復クラス
# (``FAILURE_MODE``) が一意に決める。原因軸 (failure_kind = mode 値 /
# failure_reason = reason 値) も同じ provider error から導出する。Stage 4/5 と
# 異なり 3-way なのは DROP (記事削除) を持つため (TARGET_REJECTED → Drop)。
#
# 新しい provider error 種別を追加しても、回復クラスを宣言していれば本 mapper は
# そのまま機能する。state でも content でもない裸の ``AIProviderError`` を渡すと
# ``TypeError`` で fail-fast する。


def map_provider_to_curation(exc: AIProviderError) -> CurationError:
    """provider 例外を Stage 3 marker に詰め替える。"""
    if not isinstance(exc, AIProviderStateError | AIProviderContentError):
        raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
    mode = exc.FAILURE_MODE
    marker: type[
        CurationRecoverableError | CurationTerminalKeepError | CurationTerminalDropError
    ]
    if mode.retryable:
        marker = CurationRecoverableError
    elif mode is AIProviderFailureMode.TARGET_REJECTED:
        marker = CurationTerminalDropError
    else:
        marker = CurationTerminalKeepError
    return marker(
        code=exc.CODE,
        failure_kind=mode.value,
        failure_reason=exc.reason.value if exc.reason is not None else None,
        provider_error=exc,
    )
