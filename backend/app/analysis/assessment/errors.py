"""Stage 4 (Assessment) ドメインエラー定義 — Layer 1 / 2-A / 2-B を本ファイルに集約。

Stage 4 で raise されうる例外と、外部 BC (``AIProviderError``) を Stage 4 marker に
詰め替える ACL を 1 ファイルにまとめる。spec の Layer 整理がそのまま本ファイルの
section に対応する:

- **Layer 1 marker**: Stage 4 task 層の **唯一の dispatch 軸**。Stage 4 で raise
  されうる全例外がこの 2 種のどちらかを継承する。Stage 共通 marker は **持たない**
  (原則 2: Stage 共通 marker は作らない、Stage 3 / Stage 5 と同思想)。
- **Layer 2-B (Stage 4 工程由来)**: assessor 内部の schema 不整合 / catalog 不一致
  など、provider 例外でない Stage 4 specific failure。Layer 1 marker を直接継承し、
  ``provider_error=None`` で marker を再利用する。
- **Layer 2-A ACL (provider 由来の詰め替え)**: ACL mapper
  ``map_provider_to_assessment`` が ``AIProviderError`` を Layer 1 marker に詰め替え、
  ``provider_error`` instance attr に元の ``AIProviderError`` を保持する。OpenAI evals
  の ``OPENAI_TIMEOUT_EXCEPTIONS`` 流に、Stage 4 の解釈を tuple 2 つに集約する。

設計詳細: ``specs/pipeline-events-stage4-assessment.md`` §Layer 1 marker / §Layer 2-A

Phase 4: Layer 1 marker を ``VectorDomainError`` 継承 + kwargs-only constructor
に締めて、``__str__`` 経路 (Logfire span ``exception.message``) から PII を
構造的に排除する。``str(exc)`` で構築していた message 引数は ACL / Layer 2-B
ともに撤去する。
"""

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
    """Stage 4 全例外の共通基底。

    task 層は本クラスでなく ``AssessmentRecoverableError`` /
    ``AssessmentTerminalSkipError`` を except する。``AssessmentError`` は
    型階層上の祖先として保持し (Stage 4 例外の identity)、catch には使わない。
    """

    STAGE: ClassVar[Stage] = Stage.ASSESSMENT


class AssessmentRecoverableError(AssessmentError):
    """将来の再実行で成功する可能性がある Stage 4 失敗。

    現状は taskiq の cron 救済 (単純 retry) で消化する。inline retry の判定軸は
    logfire 設計で詰める (本 spec では持たない)。

    Phase 4: constructor は kwargs-only。``message`` 引数は廃止し ``__str__``
    は ``AssessmentRecoverableError(code='...')`` 固定形式になる。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 4 specific は
            ``"assessment_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持 (audit forensics + ``__cause__`` 連鎖)。
            Stage 4 specific (Layer 2-B) では ``None``。
    """

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
    """リトライ無効、現状の curation では assess できないと諦める Stage 4 失敗。

    article / curation は保持、assessment 行は作らず audit を焼いて return する。
    "Terminal" は「これ以上の試行は無意味、終端」、"Skip" は「assessment を作らず
    skip する」の意。

    Phase 4: kwargs-only constructor、``__str__`` は SAFE_ATTRS=(``code``,) のみ。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 4 specific は
            ``"assessment_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持。Stage 4 specific (Layer 2-B) では ``None``。
    """

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
    """AI 応答が Stage 4 schema に合致しない (Layer 2-B、Stage 4 工程由来)。

    具体的には assessor 内部の ``parse_assessment`` で:
    - 必須 key (``category`` / ``investor_take`` / ``events``) 欠落
    - 値が ``str`` 型でない (``isinstance`` 検証で reject)
    - ``category`` が ``ValidCategory`` enum 外の値
    - Pydantic ``ValidationError`` (``min_length`` 違反等)

    AI モデルの揺らぎ (構造化出力でも稀に schema を外す) で発生、cron 救済で
    現実的に回復する見込み。``provider_error=None`` で marker を継承
    (provider 例外起源ではないため)。

    Phase 4: 旧 ``message`` 引数は廃止 (PII 含有経路)。
    """

    def __init__(self) -> None:
        super().__init__(
            code="assessment_response_invalid",
            provider_error=None,
        )


class AssessmentCategoryMissingError(AssessmentTerminalSkipError):
    """AI が category catalog に存在しない slug を返した (Layer 2-B)。

    catalog 側の追加または prompt 側の category 列挙不一致が原因。retry しても
    AI は同じ slug を返し続けるので terminal-skip。catalog を拡張すれば解消。
    ``AssessmentRepository.save_in_scope`` の slug → id 解決失敗で raise される。

    Phase 4: 旧 ``message`` 引数は廃止 (PII 含有経路 — message に category slug が
    入る経路があった)。具体 slug は repository 側で structlog の attribute
    として stdout には残せるが、``__str__`` 経由 (Logfire span) には流さない。
    """

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
    """provider 例外を Stage 4 marker に詰め替える (Anti-Corruption Layer)。

    Stage 4 boundary (``AssessmentService.execute``) で呼ぶ。``AIProviderError`` の
    subclass で上記 2 tuple のいずれにも未登録のものは ``TypeError`` を raise する
    (新規 provider error 種別の登録漏れを deploy 前に検知する fail-fast)。

    Phase 4: ``str(exc)`` を marker constructor に渡していた旧経路は廃止
    (Layer 1 marker が kwargs-only に締まったため、SDK message が ``__str__``
    に乗らない構造)。

    Args:
        exc: assessor 層が raise した ``AIProviderError`` instance。

    Returns:
        Stage 4 marker (``AssessmentRecoverableError`` /
        ``AssessmentTerminalSkipError``) の instance。``provider_error`` attr に元
        ``exc`` を identity 付きで保持。``code`` attr は元 ``exc.CODE`` を引き継ぐ
        (audit ラベル連鎖)。

    Raises:
        TypeError: ``AIProviderError`` subclass がどちらの tuple にも未登録の場合。
    """
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
