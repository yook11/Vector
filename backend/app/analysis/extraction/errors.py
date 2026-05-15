"""Stage 3 (Extraction) ドメインエラー定義 — Layer 1 / 2-A / 2-B を本ファイルに集約。

Stage 4 (assessment) / Stage 5 (embedding) と対称の構造を取る。Stage 3 で raise
されうる例外と、外部 BC (``AIProviderError``) を Stage 3 marker に詰め替える ACL
を 1 ファイルにまとめる。

- **Layer 1 marker**: Stage 3 task 層の dispatch 軸。Stage 3 は article DELETE /
  Keep / Retryable の 3 挙動を持つので 3 軸を持つ (Stage 4/5 は 2 軸)。Stage 共通
  marker は **持たない** (原則 2、Stage 4/5 と同思想)。
- **Layer 2-B (Stage 3 工程由来)**: extractor 内部の schema 不整合など、provider
  例外でない Stage 3 specific failure。Layer 1 marker を直接継承し、
  ``provider_error=None`` で marker を再利用する。
- **Layer 2-A ACL (provider 由来の詰め替え)**: ACL mapper
  ``map_provider_to_extraction`` が ``AIProviderError`` を Layer 1 marker に詰め
  替え、``provider_error`` instance attr に元 ``AIProviderError`` を保持する。
  Stage 3 の解釈を tuple 3 つ (Drop / Keep / Recoverable) に集約する。

設計詳細: ``specs/pipeline-events-error-taxonomy.md`` §Layer 1 marker / §Layer 2-A
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
# Layer 1 marker (Stage 3 task 層の dispatch 軸、3 axis)
# ---------------------------------------------------------------------------


class ExtractionError(Exception):
    """Stage 3 全例外の共通基底。

    task 層は本クラスでなく ``ExtractionRecoverableError`` /
    ``ExtractionTerminalKeepError`` / ``ExtractionTerminalDropError`` を except
    する。``ExtractionError`` は型階層上の祖先として保持し (Stage 3 例外の
    identity)、catch には使わない。
    """


class ExtractionRecoverableError(ExtractionError):
    """将来の再実行で成功する可能性がある Stage 3 失敗。

    一時障害 (network / service unavailable / rate limited / quota) や schema
    違反 (parse 不能) など。taskiq retry の上限後は cron 救済で消化する
    (旧 INLINE_RETRY 軸は廃止、Stage 4/5 と統一)。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 3 specific は
            ``"extraction_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持 (audit forensics + ``__cause__`` 連鎖)。
            Stage 3 specific (Layer 2-B) では ``None``。
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


class ExtractionTerminalKeepError(ExtractionError):
    """retry 無意味、article は保持する Stage 3 失敗。

    configuration / request invalid / insufficient balance など、運用側修正で
    復旧する系統。article DELETE せず audit のみ焼いて return する。運用者が
    根本原因 (API key / 残高など) を直したあと cron で再 dispatch される。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぐ。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持。
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


class ExtractionTerminalDropError(ExtractionError):
    """retry 無意味、article DELETE 対象の Stage 3 失敗。

    provider が入力を明示的に拒否した (input rejected) / 出力を policy 抑制した
    (output blocked) ケース。記事自体に問題があり、別 model / 再試行でも通らない
    ため audit 後 article を repository から削除する。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぐ。
        provider_error: 元 ``AIProviderError`` instance を identity 付きで保持。
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
# Layer 2-B (Stage 3 工程由来)
# ---------------------------------------------------------------------------


class ExtractionResponseInvalidError(ExtractionRecoverableError):
    """AI 応答が Stage 3 schema に合致しない (Layer 2-B、Stage 3 工程由来)。

    具体的には extractor 内部の ``parse_extraction`` / Pydantic ValidationError で:
    - 必須 field 欠落
    - 値型の不一致
    - ``response_schema`` で表現できない invariant 違反

    AI モデルの揺らぎで発生、cron 救済で現実的に回復する見込み。
    ``provider_error=None`` で marker を継承 (provider 例外起源ではないため)。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="extraction_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``ExtractionService.execute()`` の boundary で ``map_provider_to_extraction`` を
# 呼ぶ。Stage 3 は article DELETE / Keep / Recoverable の 3 軸を持つので tuple も
# 3 つに分かれる。Stage 4/5 とは tuple 数のみ異なり、構造は同じ。
#
# 新しい provider error class が追加されたら、下記の該当 tuple に 1 行追加する
# だけで Stage 3 の解釈に組み込める (コード分岐の追加は不要)。未登録の
# ``AIProviderError`` subclass で ``map_provider_to_extraction`` を呼ぶと
# ``TypeError`` で fail-fast する。


EXTRACTION_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderQuotaExhaustedError,
)
"""``ExtractionRecoverableError`` に詰め替えるべき provider error 一覧。

将来の再実行で成功する可能性があるもの (transient / rate limit / quota)。
"""


EXTRACTION_TERMINAL_KEEP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
)
"""``ExtractionTerminalKeepError`` に詰め替えるべき provider error 一覧。

retry しても同じ結果になるが article 自体は健全 (configuration / request /
balance)。article は保持し audit のみ焼く。
"""


EXTRACTION_TERMINAL_DROP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)
"""``ExtractionTerminalDropError`` に詰め替えるべき provider error 一覧。

provider が記事入力を明示拒否 or 応答を policy 抑制 = 記事自体に問題あり。
article DELETE 対象。
"""


def map_provider_to_extraction(exc: AIProviderError) -> ExtractionError:
    """provider 例外を Stage 3 marker に詰め替える (Anti-Corruption Layer)。

    Stage 3 boundary (``ExtractionService.execute`` および
    ``ReExtractionService._extract_once_mapped``) で呼ぶ。``AIProviderError`` の
    subclass で上記 3 tuple のいずれにも未登録のものは ``TypeError`` を raise する
    (新規 provider error 種別の登録漏れを deploy 前に検知する fail-fast)。

    Args:
        exc: extractor 層が raise した ``AIProviderError`` instance。

    Returns:
        Stage 3 marker (``ExtractionRecoverableError`` /
        ``ExtractionTerminalKeepError`` / ``ExtractionTerminalDropError``) の
        instance。``provider_error`` attr に元 ``exc`` を identity 付きで保持。
        ``code`` attr は元 ``exc.CODE`` を引き継ぐ (audit ラベル連鎖)。

    Raises:
        TypeError: ``AIProviderError`` subclass がどの tuple にも未登録の場合。
    """
    if isinstance(exc, EXTRACTION_RECOVERABLE_PROVIDER_ERRORS):
        return ExtractionRecoverableError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, EXTRACTION_TERMINAL_KEEP_PROVIDER_ERRORS):
        return ExtractionTerminalKeepError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, EXTRACTION_TERMINAL_DROP_PROVIDER_ERRORS):
        return ExtractionTerminalDropError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
