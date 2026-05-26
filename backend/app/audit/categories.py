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
    """``pipeline_events.category`` の取りうる値 (8 種、SSoT)。

    型階層には ``UNKNOWN`` は登場しない — catch-all (``except Exception``) で任意の
    Exception に付与する DB ラベルとしてのみ存在する。

    全 stage で例外クラス由来の **intrinsic** な性質 (retry-friendly か否か) を
    表す。retry 上限に到達したかどうかの **extrinsic** な事実は payload 側の
    ``retry_exhausted: bool | None`` に持つ (``CompletionPayload`` / ``BriefingPayload``
    precedent)。

    各値の使い分け:

    - ``SUCCESS`` — 成功 path 共通。
    - ``IDEMPOTENT_SKIP`` — 冪等 skip (主に collection 系で未活性、acquisition 系
      は非イベント扱いで沈黙)。
    - ``RETRYABLE`` — 例外クラスが intrinsic に retry-friendly (Stage 3-5 の
      ``ExtractionRecoverableError`` / ``AssessmentRecoverableError`` /
      ``EmbeddingError`` family、briefing の ``openai.APIError`` /
      DB ``RUNTIME`` 系)。
    - ``NON_RETRYABLE_DROP_ARTICLE`` — curation で記事削除を伴う非 retry。
    - ``NON_RETRYABLE_KEEP_ARTICLE`` — curation で記事保持の非 retry。
    - ``NON_RETRYABLE_KEEP_CURATION`` — assessment / embedding が回復不能でも
      Stage 3 の curation 結果は保存維持する用途
      (``AssessmentTerminalSkipError`` 系 dispatch 先)。
    - ``NON_RETRYABLE`` — briefing 用、entity 固有後処理 (記事や curation の保持
      ⁄ 削除など) を伴わない汎用の非 retry。intrinsic に retry-non-friendly な
      例外 (``BriefingConfigurationError`` / pydantic ``ValidationError`` /
      DB ``CONSTRAINT`` / ``QUERY_OR_SCHEMA`` 系) に付ける。
    - ``UNKNOWN`` — 分類漏れ (catch-all)。
    """

    SUCCESS = "success"
    IDEMPOTENT_SKIP = "idempotent_skip"
    RETRYABLE = "retryable"
    NON_RETRYABLE_DROP_ARTICLE = "non_retryable_drop_article"
    NON_RETRYABLE_KEEP_ARTICLE = "non_retryable_keep_article"
    NON_RETRYABLE_KEEP_CURATION = "non_retryable_keep_curation"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"
