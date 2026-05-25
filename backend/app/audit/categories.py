"""pipeline_events の Layer 1 category 値 SSoT。

DB ``category`` カラムが取りうる 7 値 (success / idempotent_skip + 失敗 4 種 +
catch-all ``unknown``) を ``Layer1Category`` enum で固定する。

Layer 1 marker (Stage 共通の dispatch Exception) は原則 2 (Stage 共通 marker は
作らない) に従い**持たない**。各 Stage の Layer 1 marker (Stage 3:
``ExtractionRecoverableError`` 系 / Stage 4: ``AssessmentRecoverableError`` 系 /
Stage 5: ``EmbeddingError`` 系) は各 Stage package 配下の ``errors.py`` に置く。
provider 由来 (Layer 2-A) の ``AIProviderError`` 系は ``app.analysis.errors``
配下に置き、各 Stage の ACL (``map_provider_to_extraction`` /
``map_provider_to_assessment`` / ``to_embedding_error``) で Stage marker に詰め
替える。

詳細: ``specs/pipeline-events-error-taxonomy.md``
"""

from __future__ import annotations

from enum import StrEnum


class Layer1Category(StrEnum):
    """``pipeline_events.category`` の取りうる値 (7 種、SSoT)。

    型階層には ``UNKNOWN`` は登場しない — catch-all (``except Exception``) で任意の
    Exception に付与する DB ラベルとしてのみ存在する。

    ``NON_RETRYABLE_KEEP_CURATION`` は assessment が回復不能でも curation
    結果は保存維持する用途 (``AssessmentTerminalSkipError`` の dispatch 先)。
    """

    SUCCESS = "success"
    IDEMPOTENT_SKIP = "idempotent_skip"
    RETRYABLE = "retryable"
    NON_RETRYABLE_DROP_ARTICLE = "non_retryable_drop_article"
    NON_RETRYABLE_KEEP_ARTICLE = "non_retryable_keep_article"
    NON_RETRYABLE_KEEP_CURATION = "non_retryable_keep_curation"
    UNKNOWN = "unknown"
