"""Layer 2-B Stage 3: Extraction 工程固有のエラー。

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
