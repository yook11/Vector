"""pipeline_events の Layer 1 dispatch marker (error taxonomy)。

Task 層が catch する 5 種の dispatch marker (Exception 3 + Outcome 基底 2) と
DB ``category`` カラムが取りうる 6 値 (5 + catch-all ``unknown``) を定義する。

Layer 2 (origin 軸) の具体型は ``app.analysis.errors`` / ``app.collection.errors``
配下に配置し、本ファイルの marker と多重継承して dispatch 軸を表現する。

詳細: ``specs/pipeline-events-error-taxonomy.md``
"""

from __future__ import annotations

from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# 成功 Outcome 基底 — Service が return、Task は分岐後 chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SuccessOutcome:
    """Stage が期待された成功状態に到達した Outcome 基底。

    下流へ chain するかどうかは具体 Outcome が表す
    (例: ``ExtractedOutcome`` は chain、``NoiseOutcome`` は別テーブル永続化済で
    chain しない)。具体型は本クラスを継承し ``CODE: ClassVar[str]`` を pin する。
    """

    CODE: ClassVar[str]


@dataclass(frozen=True, slots=True)
class IdempotentSkipOutcome:
    """Stage が「既に処理済みで何もしなかった」べき等スキップを表す Outcome 基底。

    具体型 (例: ``AlreadyExtractedOutcome``) は本クラスを継承し
    ``CODE: ClassVar[str]`` を pin する。
    """

    CODE: ClassVar[str]
