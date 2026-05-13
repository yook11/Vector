"""pipeline_events.payload の Pydantic Discriminated Union。

ADR §データモデル §Payload の宣言と一致。Stage ごとに別 variant、
``kind`` フィールドで discriminator を取る。

PR1 では ``SourceFetchPayload`` のみ実書込される。他 Stage の variant は
schema として用意するが書込は PR2-PR4 で順次活性化される。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BasePipelineEventPayload(BaseModel):
    """共通基底 — A 級保険 + S 級失敗詳細を共通化。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str
    source_name: str | None = None  # A: FK 切断耐性
    error_message: str | None = None  # S: 失敗時
    error_chain: list[str] | None = None  # S: cause chain FQN list


class DispatchPayload(BasePipelineEventPayload):
    kind: Literal["dispatch"] = "dispatch"
    dispatched_count: int | None = None
    skip_reason: Literal["no_active_sources"] | None = None


class SourceFetchPayload(BasePipelineEventPayload):
    """Stage 1 — 1 ソース 1 fetch の集約サマリ。

    PR2.5-B 以降の κ: 件数 5 種を常時 populate し、
    ``entry_count == article_created + completion_queued + skipped + failed``
    の不変条件を ``model_validator`` で fail-fast 検証する。
    breakdown dict (``*_codes``) は sparse のまま (None / 空 dict は省略)。
    """

    kind: Literal["source_fetch"] = "source_fetch"
    fetcher_class: str | None = None  # A: type(fetcher).__name__

    # 件数集計 (常時 populate、デフォルト 0)
    entry_count: int = 0
    article_created_count: int = 0  # Pattern R 直接永続化数
    completion_queued_count: int = 0  # Pattern H pending 投入数
    skipped_count: int = 0  # known_url / race 敗北等
    failed_count: int = 0  # エントリ単位 Failed 数

    # 内訳 (sparse、None で省略)
    completion_reason_codes: dict[str, int] | None = None  # 例 {"html_required": N}
    skipped_codes: dict[str, int] | None = None  # 例 {"known_url": N}
    failed_codes: dict[str, int] | None = None  # Failed.reason.code 別カウント

    # 「このソースが何を提供しているか」 (PR1.5 で activate)
    metadata_fields_observed: list[str] | None = None  # A
    metadata_sample: dict[str, Any] | None = None  # A'

    # 失敗時 S 級 snapshot (Task 例外パスで詰める)
    http_status: int | None = None
    final_url: str | None = None
    response_size: int | None = None
    content_type: str | None = None
    body_head: str | None = None  # 先頭 500 字

    @model_validator(mode="after")
    def _check_entry_count_invariant(self) -> SourceFetchPayload:
        total = (
            self.article_created_count
            + self.completion_queued_count
            + self.skipped_count
            + self.failed_count
        )
        if self.entry_count != total:
            raise ValueError(
                f"entry_count={self.entry_count} != article_created+"
                f"completion_queued+skipped+failed={total}"
            )
        return self


class ContentFetchPayload(BasePipelineEventPayload):
    """Stage 2 — 1 記事 1 HTML 取得。

    集計 key は ``canonical_url`` (= pending.url / articles.source_url の
    SSoT 値)。``articles.id`` は別途 ``article_id`` カラム (pipeline_events)
    で関連付ける。
    """

    kind: Literal["content_fetch"] = "content_fetch"
    # A: pending → article をまたぐ canonicalize 済み URL key
    canonical_url: str | None = None
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
    # A: prompt+model+gen_config+response_schema+system_instruction の SHA-256 prefix 8
    # (Prompt class が ClassVar で確定。詳細は ADR §prompt_version の規律)
    prompt_version: str | None = None

    # 入力 (外部由来 raw、article.original_content 経由)
    input_content_head: str | None = None  # S: 先頭 2KB
    input_content_length: int | None = None  # A': 全体長 (truncate 検知)
    input_content_hash: str | None = None  # A: sha256 prefix 16 文字

    # 出力 (AI raw、Vector 内のどこにも残らない極めて貴重な情報)
    ai_raw_response: str | None = None  # S: 2KB 上限

    # A 級: AI 応答の生メタデータ (詰め替え前生値、Stage 4 raw_category と対称)
    raw_relevance: str | None = None  # signal / noise / それ以外の AI 生値


class AssessmentPayload(BasePipelineEventPayload):
    """Stage 4 (assessment) — 入力が小さい (記事サマリ) ので full 保存。

    PR5: ``ClassificationPayload`` を本クラスに置換 (class 名と discriminator
    値の一致を回復)。spec ``specs/pipeline-events-stage4-assessment.md``
    §AssessmentPayload SSoT に準拠 (14 field)。caller (PR6 で Service / Task)
    はまだいない dead code。

    state representation を持たない (top-level column の ``article_id`` /
    ``outcome_code`` / ``category`` / ``code`` / ``event_type`` / ``attempt``
    と二重化禁止)。state は top-level 4 軸 (event_type / outcome_code /
    category / code) で完全識別可能で、payload は詳細情報のみ。

    audit は witness — AI 境界で起きた事実を証言する。事後に採番された PK や
    その時点で偶然 FK が指していた id は事実ではなく操作的副産物なので保持しない
    (詳細は ``specs/backlog/audit-payload-fact-purification.md``)。

    状態識別:

    - in-scope 成功: ``category_slug`` / ``investor_take`` が非 None
    - out-of-scope 成功: ``investor_take`` のみ非 None
      (in-scope 系 ``category_slug`` は None)
    - 失敗: Base の ``error_message`` / ``error_chain`` (+ 該当時 ``ai_raw_response``)
    """

    kind: Literal["assessment"] = "assessment"

    # Stage 4 固有 identifier (top-level column が無いため payload で保持、自然キー)
    extraction_id: int | None = None

    # A 級: メタデータ
    ai_model: str | None = None  # 使用 assessor の model 名
    prompt_version: str | None = None  # prompt+model+config の SHA-256 prefix 8

    # A' / S 級: AI 入出力 (Stage 4 = input full 4KB + raw 2KB)
    input_text: str | None = None  # 入力 summary 全文 (4KB 上限)
    input_text_length: int | None = None  # truncate 検知用
    ai_raw_response: str | None = None  # AI raw JSON response (2KB 上限)

    # A 級: AI 応答の生メタデータ (validation 前、failure forensics 用)
    raw_category: str | None = None  # AI が返した未検証 category slug

    # A 級: 成功時の AI 応答 (検証通過後の値、失敗時は None)
    category_slug: str | None = None  # category catalog 確認後の slug
    investor_take: str | None = None  # in-scope / out-of-scope の AI コメント


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
    | AssessmentPayload
    | EmbeddingPayload,
    Field(discriminator="kind"),
]
