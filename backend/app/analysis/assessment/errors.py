"""Stage 4 assessment の marker error と provider error adapter。"""

from __future__ import annotations

from enum import StrEnum
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
from app.logfire.exceptions import VectorDomainError

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


class AssessmentTerminalError(AssessmentError):
    """再試行は無効で assessment を作らない Stage 4 失敗の共通基底。

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
        if type(self) is AssessmentTerminalError:
            raise TypeError("AssessmentTerminalError is abstract")
        super().__init__()
        self.code = code
        self.provider_error = provider_error


class AssessmentTerminalStageBlockedError(AssessmentTerminalError):
    """stage/provider 全体が停止している Stage 4 失敗。"""

    FAILURE_KIND: ClassVar[str] = "terminal_stage_blocked"


class AssessmentTerminalTargetRejectedError(AssessmentTerminalError):
    """処理対象 curation 固有の拒否により assessment を作らない失敗。"""

    FAILURE_KIND: ClassVar[str] = "terminal_target_rejected"


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 4 工程由来)
# ---------------------------------------------------------------------------


class AssessmentResponseInvalidError(AssessmentRecoverableError):
    """AI 応答が assessment として使えない (Recoverable marker)。

    marker は「AI 応答が assessment に使えない」という分類と recoverable 性だけを
    担う。「なぜ失敗したか」は ``defect`` の値が運ぶ。``defect`` は失敗を検知した
    場所 (``parse.py`` の ``AssessmentResponseDefect`` / 各 provider adapter の
    ``*ResponseDefect``) が所有する ``StrEnum`` member で、その ``value`` がそのまま
    audit の ``outcome_code`` になる (監査ステージは再分類しない)。

    本 module は検知場所の enum を import せず ``StrEnum`` で受ける (依存方向を
    検知場所 → marker に保ち、provider 追加で本 module を触らない)。型ガードにより
    自由文字列 (= AI 生成値 = PII を載せうる) を ctor に通さず、監査に焼くのは
    StrEnum member の value (種別ラベル) だけに限定する。
    """

    def __init__(self, defect: StrEnum) -> None:
        if not isinstance(defect, StrEnum):
            # PII 境界: 種別ラベル (StrEnum) 以外を code に昇格させない。
            raise TypeError("defect must be a StrEnum member")
        super().__init__(code=defect.value, provider_error=None)


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``AssessmentService.execute()`` の boundary で ``map_provider_to_assessment`` を
# 呼ぶ。Stage 4 が「どの provider error を recoverable として扱うか / stage-wide
# terminal と target-local terminal のどちらとして扱うか」を tuple 3 つに集約する
# (OpenAI evals の
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
    AIProviderUsageLimitExhaustedError,
)
"""``AssessmentRecoverableError`` に詰め替えるべき provider error 一覧。

将来の再実行で成功する可能性があるもの (transient / rate limit / usage limit)。
新しい provider error 種別を追加したら必ず本 tuple または下記 terminal tuple
のいずれかに 1 行加える運用ルール。
"""


ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
)
"""``AssessmentTerminalStageBlockedError`` に詰め替える provider error 一覧。

どの記事を投入しても同じ失敗になる stage/provider 全体の健全性問題。
観測時に assessment hold を立てる対象。
"""


ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS: tuple[
    type[AIProviderError], ...
] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
"""``AssessmentTerminalTargetRejectedError`` に詰め替える provider error 一覧。

対象 curation 固有の content/safety 拒否。stage 全体は健全なため hold しない。
"""


def map_provider_to_assessment(exc: AIProviderError) -> AssessmentError:
    """provider 例外を Stage 4 marker に詰め替える。"""
    if isinstance(exc, ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS):
        return AssessmentRecoverableError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, ASSESSMENT_TERMINAL_STAGE_BLOCKED_PROVIDER_ERRORS):
        return AssessmentTerminalStageBlockedError(
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, ASSESSMENT_TERMINAL_TARGET_REJECTED_PROVIDER_ERRORS):
        return AssessmentTerminalTargetRejectedError(
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
