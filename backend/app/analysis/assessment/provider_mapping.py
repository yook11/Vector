"""Stage 4 ACL: ``AIProviderError`` を Stage 4 marker に詰め替える mapper。

Service.execute() の boundary で呼ぶ。Stage 4 が「どの provider error を
recoverable として扱うか / terminal-skip として扱うか」を tuple 2 つに集約する
(OpenAI evals の ``OPENAI_TIMEOUT_EXCEPTIONS`` 流)。Stage 3 が別の方針を持ちたければ
Stage 3 専用の tuple を作る (本 PR では Stage 3 経路は touch しない)。

新しい provider error class が追加されたら、本ファイルの該当 tuple に 1 行
追加するだけで Stage 4 の解釈に組み込める (コード分岐の追加は不要)。未登録の
``AIProviderError`` subclass で ``map_provider_to_assessment`` を呼ぶと ``TypeError``
で fail-fast する。

設計詳細: ``specs/pipeline-events-stage4-assessment.md`` §Layer 2-A → Stage 4 marker
"""

from __future__ import annotations

from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.errors.provider import (
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
extraction は保持し、assessment 行は作らず audit を焼いて skip する。
"""


def map_provider_to_assessment(exc: AIProviderError) -> AssessmentError:
    """provider 例外を Stage 4 marker に詰め替える (Anti-Corruption Layer)。

    Stage 4 boundary (``AssessmentService.execute``) で呼ぶ。``AIProviderError`` の
    subclass で上記 2 tuple のいずれにも未登録のものは ``TypeError`` を raise する
    (新規 provider error 種別の登録漏れを deploy 前に検知する fail-fast)。

    Args:
        exc: classifier 層が raise した ``AIProviderError`` instance。

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
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS):
        return AssessmentTerminalSkipError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
