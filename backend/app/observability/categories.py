"""pipeline_events の Layer 1 dispatch marker (error taxonomy)。

Task 層が catch する 3 種の Exception dispatch marker と DB ``category``
カラムが取りうる 7 値 (success / idempotent_skip + 失敗 4 種 + catch-all
``unknown``) を定義する。成功 Outcome 基底 (``SuccessOutcome`` /
``IdempotentSkipOutcome``) は Stage 4 Assessment / Stage 5 Embedding / Stage 3
Extraction の戻り値が全て ``int | None`` ベースに統一されたため廃止済。

Layer 2 (origin 軸) の具体型は ``app.analysis.errors`` / ``app.collection.errors``
配下に配置し、本ファイルの marker と多重継承して dispatch 軸を表現する。

詳細: ``specs/pipeline-events-error-taxonomy.md``
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class Layer1Category(StrEnum):
    """``pipeline_events.category`` の取りうる値 (7 種、SSoT)。

    型階層には ``UNKNOWN`` は登場しない — catch-all (``except Exception``) で任意の
    Exception に付与する DB ラベルとしてのみ存在する。

    PR4: ``NON_RETRYABLE_KEEP_EXTRACTION`` を追加。assessment が回復不能でも
    extraction 結果は保存維持する用途 (``AssessmentTerminalSkipError`` の dispatch
    先、PR5 で ``AssessmentAuditRepository._category_of`` から参照される)。
    """

    SUCCESS = "success"
    IDEMPOTENT_SKIP = "idempotent_skip"
    RETRYABLE = "retryable"
    NON_RETRYABLE_DROP_ARTICLE = "non_retryable_drop_article"
    NON_RETRYABLE_KEEP_ARTICLE = "non_retryable_keep_article"
    NON_RETRYABLE_KEEP_EXTRACTION = "non_retryable_keep_extraction"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 失敗 marker (Exception 系) — Task 層で 3 except + catch-all で dispatch
# ---------------------------------------------------------------------------


class RetryableError(Exception):
    """retry すれば回復が見込める失敗 (一時障害 / format 違反等)。

    ``INLINE_RETRY`` が ``True`` なら taskiq の標準 retry に乗せる (``raise`` で
    再投入)。``False`` なら inline retry は打ち切り、即 audit + return して cron
    TTL 救済に任せる (例: rate limited / quota exhausted は近い tick で回復しないので
    即諦める)。

    具体型側で必ず ``ClassVar`` で pin すること (基底のデフォルトには依拠させない)。
    """

    INLINE_RETRY: ClassVar[bool]


class NonRetryableDropArticle(Exception):
    """retry しても回復見込みなし、Article 救済不可 (provider 明示拒否等)。

    Task 層は ``Service.mark_article_unprocessable(...)`` を呼んで Article を mark
    し、audit 後 return する。``AIProviderInputRejectedError`` /
    ``AIProviderOutputBlockedError`` の 2 種に厳密化されている
    (詳細: spec §設計原則 1)。
    """


class NonRetryableKeepArticle(Exception):
    """retry しても回復見込みなし、Article 自体は次回 retry で救済可能 (環境起因)。

    Task 層は Article を mark せず audit 後 return する (cron TTL 削除に渡さない、
    運用が根本原因を直したあと再 dispatch で救済される)。
    """
