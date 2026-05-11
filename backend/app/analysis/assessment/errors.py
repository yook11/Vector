"""Stage 4 (Assessment) 例外階層 — Layer 1 marker 2 種。

Stage 4 task 層の **唯一の dispatch 軸**。Stage 4 で raise されうる全例外がこの 2 種の
どちらかを継承する。foundation marker (``RetryableError`` / ``NonRetryableKeepArticle``
等) は **継承しない** (原則 2: Stage 共通 marker は作らない)。

provider 由来の場合は ACL mapper (``provider_mapping.map_provider_to_assessment``)
が本 marker を直接 instantiate して ``provider_error`` instance attr に元の
``AIProviderError`` を保持する。Stage 4 specific (Layer 2-B、PR2 で追加予定) は
``provider_error=None`` で同じ marker を再利用する。

設計詳細: ``specs/pipeline-events-stage4-assessment.md`` §Layer 1 marker
"""

from __future__ import annotations

from app.analysis.errors.provider import AIProviderError


class AssessmentError(Exception):
    """Stage 4 全例外の共通基底。

    task 層は本クラスでなく ``AssessmentRecoverableError`` /
    ``AssessmentTerminalSkipError`` を except する。``AssessmentError`` は
    型階層上の祖先として保持し (Stage 4 例外の identity)、catch には使わない。
    """


class AssessmentRecoverableError(AssessmentError):
    """将来の再実行で成功する可能性がある Stage 4 失敗。

    現状は taskiq の cron 救済 (単純 retry) で消化する。inline retry の判定軸は
    logfire 設計で詰める (本 spec では持たない)。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 4 specific は
            ``"assessment_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持 (audit forensics + ``__cause__`` 連鎖)。
            Stage 4 specific (Layer 2-B) では ``None``。
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


class AssessmentTerminalSkipError(AssessmentError):
    """リトライ無効、現状の extraction では assess できないと諦める Stage 4 失敗。

    article / extraction は保持、assessment 行は作らず audit を焼いて return する。
    "Terminal" は「これ以上の試行は無意味、終端」、"Skip" は「assessment を作らず
    skip する」の意。

    Attributes:
        code: audit ラベル (``pipeline_events.code`` 列に直接書き込む)。
            provider 由来は ``exc.CODE`` を引き継ぎ、Stage 4 specific は
            ``"assessment_*"`` を pin。
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を
            identity 付きで保持。Stage 4 specific (Layer 2-B) では ``None``。
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
# Layer 2-B (Stage 4 工程由来、PR2 で追加)
# ---------------------------------------------------------------------------


class AssessmentResponseInvalidError(AssessmentRecoverableError):
    """AI 応答が Stage 4 schema に合致しない (Layer 2-B、Stage 4 工程由来)。

    具体的には assessor 内部の ``parse_assessment`` で:
    - 必須 key (``category`` / ``topic`` / ``investor_take``) 欠落
    - 値が ``str`` 型でない (``isinstance`` 検証で reject)
    - ``category`` が ``ValidCategory`` enum 外の値
    - Pydantic ``ValidationError`` (``min_length`` 違反 / ``TopicName`` 制約違反)

    AI モデルの揺らぎ (構造化出力でも稀に schema を外す) で発生、cron 救済で
    現実的に回復する見込み。``provider_error=None`` で marker を継承
    (provider 例外起源ではないため)。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="assessment_response_invalid",
            provider_error=None,
        )


class AssessmentCategoryMissingError(AssessmentTerminalSkipError):
    """AI が category catalog に存在しない slug を返した (Layer 2-B)。

    catalog 側の追加または prompt 側の category 列挙不一致が原因。retry しても
    AI は同じ slug を返し続けるので terminal-skip。catalog を拡張すれば解消。
    ``AssessmentRepository.save_in_scope`` の slug → id 解決失敗で raise される。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="assessment_category_missing",
            provider_error=None,
        )
