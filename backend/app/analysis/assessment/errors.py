"""Stage 4 assessment の marker error と provider error adapter。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderError,
    AIProviderStateError,
)
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire.exceptions import VectorDomainError

# ---------------------------------------------------------------------------
# Layer 1 marker (Stage 4 task 層の dispatch 軸)
# ---------------------------------------------------------------------------


class AssessmentError(VectorDomainError):
    """Stage 4 全例外の共通基底。直接の catch 対象にはしない。"""


class AssessmentRecoverableError(AssessmentError):
    """再実行で回復しうる assessment 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY``) だけ。原因軸 (``failure_kind`` =
    回復クラス / ``failure_reason`` = 詳細) は instance 値で持ち、provider error の
    ``FAILURE_MODE`` / ``reason`` から ``map_provider_to_assessment`` が導出する。
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


class AssessmentTerminalError(AssessmentError):
    """再試行は無効で assessment を作らない Stage 4 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY`` = NON_RETRYABLE) だけ。stage 全体を
    止めるか対象固有かは型では区別せず、handler が provider error の ``FAILURE_MODE``
    から hold を導出する。原因軸 (``failure_kind`` / ``failure_reason``) は
    ``AssessmentRecoverableError`` と同形の instance 値。
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


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 4 工程由来)
# ---------------------------------------------------------------------------


class AssessmentResponseInvalidError(AssessmentRecoverableError):
    """AI 応答が assessment として使えない (Recoverable marker)。

    marker は「AI 応答が assessment に使えない」という分類と recoverable 性だけを
    担う。「なぜ失敗したか」は ``defect`` の値が運ぶ。``defect`` は失敗を検知した
    場所 (``parse.py`` の ``AssessmentResponseDefect`` / 各 provider adapter の
    ``*ResponseDefect``) が所有する ``StrEnum`` member で、その ``value`` がそのまま
    audit の ``outcome_code`` になる (監査ステージは再分類しない)。原因ファミリーは
    provider 起因ではないため ``failure_kind="ai_response_invalid"`` で固定する。

    本 module は検知場所の enum を import せず ``StrEnum`` で受ける (依存方向を
    検知場所 → marker に保ち、provider 追加で本 module を触らない)。型ガードにより
    自由文字列 (= AI 生成値 = PII を載せうる) を ctor に通さず、監査に焼くのは
    StrEnum member の value (種別ラベル) だけに限定する。
    """

    def __init__(self, defect: StrEnum) -> None:
        if not isinstance(defect, StrEnum):
            # PII 境界: 種別ラベル (StrEnum) 以外を code に昇格させない。
            raise TypeError("defect must be a StrEnum member")
        super().__init__(
            code=defect.value,
            failure_kind="ai_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``AssessmentService.execute()`` の boundary で ``map_provider_to_assessment`` を
# 呼ぶ。retry 軸 (Recoverable / Terminal) は provider の回復クラス
# (``FAILURE_MODE.retryable``) が一意に決め、原因軸 (failure_kind = mode 値 /
# failure_reason = reason 値) は同じ provider error から導出する。state / content の
# 分岐は不要 (両系統とも ``FAILURE_MODE`` を持つ)。hold (stage 退避) は marker 型では
# なく handler が mode から導出する。
#
# 新しい provider error 種別を追加しても、回復クラスを宣言していれば本 mapper は
# そのまま機能する。state でも content でもない裸の ``AIProviderError`` を渡すと
# ``TypeError`` で fail-fast する。


def map_provider_to_assessment(exc: AIProviderError) -> AssessmentError:
    """provider 例外を Stage 4 marker に詰め替える。"""
    if not isinstance(exc, AIProviderStateError | AIProviderContentError):
        raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
    mode = exc.FAILURE_MODE
    marker = AssessmentRecoverableError if mode.retryable else AssessmentTerminalError
    return marker(
        code=exc.CODE,
        failure_kind=mode.value,
        failure_reason=exc.reason.value if exc.reason is not None else None,
        provider_error=exc,
    )
