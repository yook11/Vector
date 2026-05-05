"""pipeline_events.payload の Pydantic Discriminated Union。

ADR §データモデル §Payload の宣言と一致。Stage ごとに別 variant、
``kind`` フィールドで discriminator を取る。

PR1 では ``SourceFetchPayload`` のみ実書込される。他 Stage の variant は
schema として用意するが書込は PR2-PR4 で順次活性化される。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BasePipelineEventPayload(BaseModel):
    """共通基底 — A 級保険 + S 級失敗詳細を共通化。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    source_name: str | None = None  # A: FK 切断耐性
    error_message: str | None = None  # S: 失敗時
    error_chain: list[str] | None = None  # S: cause chain FQN list


class DispatchPayload(BasePipelineEventPayload):
    kind: Literal["dispatch"] = "dispatch"
    dispatched_count: int | None = None
    skip_reason: Literal["no_active_sources"] | None = None


class SourceFetchPayload(BasePipelineEventPayload):
    """Stage 1 — 1 ソース 1 fetch の集約サマリ。"""

    kind: Literal["source_fetch"] = "source_fetch"
    fetcher_class: str | None = None  # A: type(fetcher).__name__
    persisted_count: int | None = None  # A' (Pattern R 永続化数)
    staged_count: int | None = None  # A' (Pattern H staged 数)
    failed_count: int | None = None  # A' (エントリ単位 Failed 数)
    skipped_count: int | None = None  # A' (race 敗北等)
    failed_codes: dict[str, int] | None = None  # S: Failed.reason.code 別カウント

    # 「このソースが何を提供しているか」 (PR1.5 で activate、PR1 では None)
    metadata_fields_observed: list[str] | None = None  # A
    metadata_sample: dict[str, Any] | None = None  # A'

    # 失敗時 S 級 snapshot (Task 例外パスで詰める、別 PR で error class 拡張時)
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None  # 先頭 500 字


class ContentFetchPayload(BasePipelineEventPayload):
    """Stage 2 — 1 記事 1 HTML 取得。"""

    kind: Literal["content_fetch"] = "content_fetch"
    # A: article 削除耐性 / PR2.5 の skip 判定 key
    discovered_article_id: int | None = None
    extractor_class: str | None = None  # A
    # S: drop 細分化 (permanent_fetch_error / extraction_empty_* / promotion_*)
    reason_code: str | None = None
    body_length: int | None = None  # A': 成功時の本文長分布観測
    # S: promotion Failed 等の {"body_length": N} 構造化メトリック
    quality_gate_metric: dict[str, Any] | None = None
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None  # 先頭 500 字


class ExtractionPayload(BasePipelineEventPayload):
    """Stage 3 — 大きい入力 (記事本文) は head + length + hash で扱う。"""

    kind: Literal["extraction"] = "extraction"
    ai_model: str | None = None  # S
    prompt_version: str | None = None  # A: deploy 時注入の git SHA

    # 入力 (外部由来 raw、article.original_content 経由)
    input_content_head: str | None = None  # S: 先頭 2KB
    input_content_length: int | None = None  # A': 全体長 (truncate 検知)
    input_content_hash: str | None = None  # A: sha256 prefix 16 文字

    # 出力 (AI raw、Vector 内のどこにも残らない極めて貴重な情報)
    ai_raw_response: str | None = None  # S: 2KB 上限

    # 解釈結果
    entity_count: int | None = None  # A'


class ClassificationPayload(BasePipelineEventPayload):
    """Stage 4 — 入力が小さい (記事サマリ) ので full 保存。"""

    kind: Literal["classification"] = "classification"
    ai_model: str | None = None  # S
    prompt_version: str | None = None  # A

    # 入力 (4KB hard limit、full)
    input_text: str | None = None  # S: full
    input_text_length: int | None = None  # A'

    # 出力
    ai_raw_response: str | None = None  # S: 2KB
    raw_category: str | None = None  # S
    raw_topic: str | None = None  # S


class EmbeddingPayload(BasePipelineEventPayload):
    """Stage 5 — analysis テキストから vector を生成。

    raw I/O 捕捉は不要 (入力は analysis に永続、出力は数値 vector で
    injection 観点無関係)。
    """

    kind: Literal["embedding"] = "embedding"
    embedding_model: str | None = None  # A
    vector_dimension: int | None = None  # A'


PipelineEventPayload = Annotated[
    DispatchPayload
    | SourceFetchPayload
    | ContentFetchPayload
    | ExtractionPayload
    | ClassificationPayload
    | EmbeddingPayload,
    Field(discriminator="kind"),
]
