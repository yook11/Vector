"""Stage 5 (Embedding) ドメインエラー定義 — Layer 1 / 2-A / 2-B を本ファイルに集約。

Stage 5 で raise されうる例外と、外部 BC (``AIProviderError``) を Stage 5 marker
に詰め替える ACL を 1 ファイルにまとめる。Stage 4 Assessment と完全同形の
``assessment/errors.py`` を雛形にした構造で、section 構成も対称:

- **Layer 1 marker**: Stage 5 task 層の **唯一の dispatch 軸**。Stage 5 で raise
  されうる全例外がこの 2 種のどちらかを継承する。foundation marker
  (``RetryableError`` / ``NonRetryableKeepArticle`` 等) は **継承しない**
  (原則 2: Stage 共通 marker は作らない、Stage 4 と同思想)。
- **Layer 2-B (Stage 5 工程由来)**: embedder 内部の応答 schema 不整合
  (``EmbeddingVector`` VO の次元 ≠ 768 / NaN / 非有限 / 範囲外) など、provider
  例外でない Stage 5 specific failure。Layer 1 marker を直接継承し、
  ``provider_error=None`` で marker を再利用する。
- **Layer 2-A ACL (provider 由来の詰め替え)**: ACL mapper
  ``to_embedding_error`` が ``AIProviderError`` を Layer 1 marker に詰め替え、
  ``provider_error`` instance attr に元の ``AIProviderError`` を保持する。Stage 4
  の ``map_provider_to_assessment`` と完全同形で、Stage 5 の解釈を tuple 2 つに
  集約する。

「TerminalSkip した事実」は ``pipeline_events.code`` カラムに上記
``AIProvider*Error`` の ``CODE`` が焼かれた行として表現される (DB 状態列の追加は
不要、Stage 4 と同思想)。
"""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Layer 1 marker (Stage 5 task 層の dispatch 軸)
# ---------------------------------------------------------------------------


class EmbeddingError(Exception):
    """Stage 5 全例外の共通基底。

    task 層は本クラスでなく ``EmbeddingRecoverableError`` /
    ``EmbeddingTerminalSkipError`` を except する。``EmbeddingError`` は
    型階層上の祖先として保持し (Stage 5 例外の identity)、catch には使わない。
    """


class EmbeddingRecoverableError(EmbeddingError):
    """将来の再実行で成功する可能性がある Stage 5 失敗。

    現状は taskiq の cron 救済 (単純 retry) で消化する。inline retry の判定軸は
    logfire 設計で詰める (本 spec では持たない)。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 5 specific は
            ``"embedding_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持 (audit forensics + ``__cause__`` 連鎖)。
            Stage 5 specific (Layer 2-B) では ``None``。
    """

    code: str
    provider_error: AIProviderError | None

    def __init__(
        self,
        message: str = "",
        *,
        code: str,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_error = provider_error


class EmbeddingTerminalSkipError(EmbeddingError):
    """リトライ無効、現状の analysis では embed できないと諦める Stage 5 失敗。

    article / extraction / analysis は保持、embedding は作らず audit を焼いて
    return する。"Terminal" は「これ以上の試行は無意味、終端」、"Skip" は
    「embedding を作らず skip する」の意。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 5 specific は
            ``"embedding_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持。Stage 5 specific (Layer 2-B) では ``None``。
    """

    code: str
    provider_error: AIProviderError | None

    def __init__(
        self,
        message: str = "",
        *,
        code: str,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_error = provider_error


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 5 工程由来)
# ---------------------------------------------------------------------------


class EmbeddingResponseInvalidError(EmbeddingRecoverableError):
    """embedder 応答が Stage 5 schema に合致しない (Layer 2-B、Stage 5 工程由来)。

    具体的には Service 内の ``EmbeddingVector`` VO 構築で:
    - 次元数 ≠ 768 (``EMBEDDING_DIMENSION``)
    - NaN / ±inf を含む (``math.isfinite`` 違反)
    - サニティ範囲 (``[-1e4, 1e4]``) 外の要素

    モデルや provider 側のバグ・揺らぎで稀に発生し、cron 救済で回復する見込み。
    ``provider_error=None`` で marker を継承 (provider 例外起源ではないため)。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="embedding_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``EmbeddingService.execute()`` の boundary で ``to_embedding_error`` を
# 呼ぶ。Stage 5 が「どの provider error を recoverable として扱うか / terminal-skip
# として扱うか」を tuple 2 つに集約する (Stage 4 ``map_provider_to_assessment`` と
# 完全同形)。
#
# 新しい provider error class が追加されたら、下記の該当 tuple に 1 行追加する
# だけで Stage 5 の解釈に組み込める (コード分岐の追加は不要)。未登録の
# ``AIProviderError`` subclass で ``to_embedding_error`` を呼ぶと
# ``TypeError`` で fail-fast する。


EMBEDDING_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderQuotaExhaustedError,
)
"""``EmbeddingRecoverableError`` に詰め替えるべき provider error 一覧。

将来の再実行で成功する可能性があるもの (transient / rate limit / quota)。
新しい provider error 種別を追加したら必ず本 tuple または下記 terminal-skip tuple
のいずれかに 1 行加える運用ルール。
"""


EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
"""``EmbeddingTerminalSkipError`` に詰め替えるべき provider error 一覧。

retry しても同じ結果になる (configuration / request / balance / safety block)。
analysis は保持し、embedding は作らず audit を焼いて skip する。
"""


def to_embedding_error(exc: AIProviderError) -> EmbeddingError:
    """provider 例外を Stage 5 marker に詰め替える (Anti-Corruption Layer)。

    Stage 5 boundary (``EmbeddingService.execute``) で呼ぶ。``AIProviderError`` の
    subclass で上記 2 tuple のいずれにも未登録のものは ``TypeError`` を raise する
    (新規 provider error 種別の登録漏れを deploy 前に検知する fail-fast)。

    Args:
        exc: embedder 層が raise した ``AIProviderError`` instance。

    Returns:
        Stage 5 marker (``EmbeddingRecoverableError`` /
        ``EmbeddingTerminalSkipError``) の instance。``provider_error`` attr に元
        ``exc`` を identity 付きで保持。``code`` attr は元 ``exc.CODE`` を引き継ぐ
        (audit ラベル連鎖)。

    Raises:
        TypeError: ``AIProviderError`` subclass がどちらの tuple にも未登録の場合。
    """
    if isinstance(exc, EMBEDDING_RECOVERABLE_PROVIDER_ERRORS):
        return EmbeddingRecoverableError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, EMBEDDING_TERMINAL_SKIP_PROVIDER_ERRORS):
        return EmbeddingTerminalSkipError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
