"""Stage 3 (Extraction) ドメインエラー — Layer 2-B のみ保持。

Stage 4 (assessment) / Stage 5 (embedding) と対称に Stage package 配下に置く。
ただし Stage 3 は Layer 1 marker (Stage 固有 Recoverable/TerminalSkip) を独自に
持たず、foundation marker (``NonRetryableDropArticle`` 等) を直接多重継承する
方針なので、本ファイルは **Layer 2-B のみ** を集約する (Layer 1 / Layer 2-A
ACL は持たない)。

``ExtractionService`` が raise する。AI が応答の返却に成功しても、Stage 3 が要求
する形式 (Pydantic schema / 業務 invariant) で使えなかった場合 (parse 不能 / schema
違反 / 必須 field 欠落) を扱う。

provider 由来 (Layer 2-A) との違い: 「provider が応答を返したか」ではなく
「Stage 3 として消化可能な応答だったか」が判定軸。同じ JSON 不整合でも Stage 4/5
とは判定基準が違うため、各 Stage の Layer 2-B に分散される (詳細: spec §設計原則 1)。

詳細: ``specs/pipeline-events-error-taxonomy.md`` §Layer 2-B
"""

from __future__ import annotations

from typing import ClassVar

from app.observability.categories import RetryableError


class ExtractionDomainError(Exception):
    """Stage 3 (Extraction) ドメインエラーの基底。

    具体型は本クラスと Layer 1 marker の多重継承で定義する。
    """


class ExtractionResponseInvalidError(ExtractionDomainError, RetryableError):
    """AI 応答が Stage 3 schema に合致しない (parse 不能 / 必須 field 欠落 / 型違反)。

    AI モデル応答の揺れで retry 救済が現実的に効くため Retryable 側に置く。
    inline retry 上限到達分は記事保持 + cron TTL で掃除 (DROP しない)。
    """

    CODE: ClassVar[str] = "extraction_response_invalid"
    INLINE_RETRY: ClassVar[bool] = True
