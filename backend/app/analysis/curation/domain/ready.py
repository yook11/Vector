"""ReadyForCuration — Stage 3 実行可能状態の precondition 型 (案 3: 厚い Ready)。

Stage 3 BC 実装。Stage 3 operation の前提条件 (Article 存在 +
``article_curations`` 未生成 + ``curation_noises`` 未生成 + 本文サイズが
system hard cap 以内) を構造保証し、かつ curator 入力 (title / content) も
含めて運ぶ厚い Ready。

設計方針 (2026-05-11 確定、案 3): Ready 型は **処理に必要な値の全揃え** を構造保証する
厚い型であり、**下流 Stage 自身 (Stage 3 Task) が処理開始時に DB から内容を fetch
して構築** する。上流 Stage 2 (collection / maintenance backfill) から Stage 3 への
kiq message は ID のみ運ぶ ``CurationTrigger`` を用い、Stage 3 Task が
``Ready.try_advance_from`` を呼んで最新の DB 状態から Ready を構築する。

Stage 4 (Assessment) / Stage 5 (Embedding) と完全同型: 上流は Trigger を kiq に詰めて
enqueue、下流 Task が処理開始時に Repository の atomic 1 query で precondition +
audit / curator 入力値を取得して Ready を構築する。

詳細は memory `project_typed_pipeline_preconditions.md` (2026-05-11 確定版)。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
詳細は memory `feedback_taskiq_basemodel_required.md`。

`MAX_CONTENT_LENGTH` は system 不変条件としての hard cap (リソース保護) であり、
adapter 固有の入力整形 (例: GeminiCurationPrompt.CONTENT_MAX_LENGTH = 20_000) と
責務が異なる。前者は「ここを超える本文は Stage 3 の対象外」を表し、後者は
「特定モデルにこのサイズで投げる」を表す。
"""

from __future__ import annotations

from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field


class CurationPreconditionProtocol(Protocol):
    """Stage 3 進行判定用 Curation Repository contract。

    「Ready 構築に必要なデータをロードする」= 「ReadyForCuration を満たす」
    という意味論で、Repository は precondition を満たす場合に
    ``ReadyForCuration`` を atomic な 1 query で構築して返す責務を持つ。
    `try_advance_from` は本 Protocol への thin delegate (Stage 4
    ``AssessmentPreconditionProtocol`` と同型)。
    """

    async def try_load_for_curation(
        self, article_id: int
    ) -> ReadyForCuration | None: ...


class ReadyForCuration(BaseModel):
    """Stage 3 curation を実行可能な状態を表す precondition 型 (厚い Ready)。

    フィールドは operation を特定する ID と、curator 入力 (title / content) の全揃え。
    Ready が存在する = 処理開始時点で DB から値を取得済 + Article 行存在 +
    signal/noise 未生成 + 本文サイズ ≤ hard cap が verify された状態 (時間ずれゼロ)。

    Invariants:
    - ``article_id``: 正の整数 (DB の Article.id を指す)
    - ``original_title``: 非空 (構築時 ``Field(min_length=1)`` で保証)
    - ``original_content``: 非空かつ ``MAX_CONTENT_LENGTH`` 以内
      (構築時 ``Field(min_length=1, max_length=...)`` で保証)
    - frozen: 生成後は不変 (Stage 間 passport として副作用なしに受け渡せる)
    """

    model_config = ConfigDict(frozen=True)

    MAX_CONTENT_LENGTH: ClassVar[int] = 200_000

    article_id: int = Field(gt=0)
    original_title: str = Field(min_length=1)
    original_content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)

    @classmethod
    async def try_advance_from(
        cls,
        *,
        article_id: int,
        repo: CurationPreconditionProtocol,
    ) -> ReadyForCuration | None:
        """article_id から Stage 3 へ advance できるかを判定する gatekeeper。

        Precondition (Stage 3 に進める条件):
        - 同 article_id の Article 行が存在
        - 同 article_id の ``article_curations`` 行が未生成
        - 同 article_id の ``curation_noises`` 行が未生成 (Stage 1 で既に
          noise 判定済の記事を再処理しない)
        - 本文長が ``MAX_CONTENT_LENGTH`` 以内 (system hard cap)

        本 method は Domain 層の named gateway として
        `Repository.try_load_for_curation` にそのまま delegate する。
        Repository が atomic な 1 query で precondition 判定 + 厚い Ready の
        構築を完結させる (Stage 4 ``ReadyForAssessment.try_advance_from`` と同型)。

        Returns:
            進める場合: `ReadyForCuration` (curator 入力値を含む厚い型)
            進めない場合: `None` (業務正常状態、例外ではない)

        Args:
            article_id: 上流 Stage 2 (collection / maintenance) で永続化された
                Article.id
            repo: ``try_load_for_curation`` を備える Repository
        """
        return await repo.try_load_for_curation(article_id)


class CurationTrigger(BaseModel):
    """Stage 3 起動 trigger — kiq message 用の軽量 ID キャリア。

    precondition は保証せず ``article_id`` のみを運ぶ。下流 Stage 3 Task が
    ``ReadyForCuration.try_advance_from`` を呼んで処理開始時に最新の DB 状態から
    Ready を構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

    上流 (collection ingest_source / scrape_html_body / maintenance backfill) は値
    fetch を行わず本 Trigger に ID だけ詰めて kiq に enqueue する。これにより
    kiq message が軽量になり、かつ enqueue → 実行までの時間ずれの影響を受けない
    (Ready 構築時に最新の DB 状態を反映する)。

    taskiq formatter は Pydantic BaseModel(frozen=True) を要求するため
    ``BaseModel`` 派生 (memory `feedback_taskiq_basemodel_required.md`)。

    Rolling deploy 互換: Pydantic の既定 ``extra='ignore'`` により、旧
    ``ReadyForCuration`` (3 fields: article_id / original_title /
    original_content) の in-flight message を新 worker が受信しても
    ``article_id`` だけ取り出して処理できる。
    """

    model_config = ConfigDict(frozen=True)

    article_id: int = Field(gt=0)
