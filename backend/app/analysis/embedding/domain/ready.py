"""ReadyForEmbedding — Stage 5 実行可能状態の precondition 型 (案 3: 厚い Ready)。

spec `specs/typed-pipeline-preconditions.md` (案 3 で再ドラフト予定) の embedding
BC 実装。Stage 5 operation の前提条件 (Embedding 未生成) を構造保証し、かつ
embedder 入力 text を保持する厚い Ready として運ぶ。

設計方針 (2026-05-12 確定、案 3): Ready 型は **処理に必要な値の全揃え** を構造保証する
厚い型であり、**下流 Stage 自身 (Stage 5 Task) が処理開始時に DB から内容を fetch
して構築** する。上流 Stage 4 Task から Stage 5 への kiq message は ID のみ運ぶ
``EmbeddingTrigger`` を用い、Stage 5 Task が ``Ready.try_advance_from`` を呼んで
最新の DB 状態から Ready を構築する。

旧 Pattern A' (ID-only Ready) は以下 3 点で型として保証の実体が無いため撤回:

1. `int` の nominal wrapper で中身に invariant が無い (`InScope` のような
   型としての価値を持たない)
2. kiq enqueue → 実行までに DB 状態が変わるため、Service が DB fetch +
   None チェックで実質的に precondition を再検証 → 「Service の precondition 分岐を
   消す」当初目的に反する
3. 「下流で次に進むことを上流が保証する」設計で責務の主語が間違っている

詳細は memory `project_typed_pipeline_preconditions.md` (2026-05-11 確定版)。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
詳細は memory `feedback_taskiq_basemodel_required.md`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingPreconditionProtocol(Protocol):
    """Stage 5 進行判定用 Embedding Repository contract。

    「Ready 構築に必要なデータをロードする」= 「ReadyForEmbedding を満たす」
    という意味論で、Repository は precondition を満たす場合に
    ``ReadyForEmbedding`` を atomic な 1 query で構築して返す責務を持つ。
    `try_advance_from` は本 Protocol への thin delegate。
    """

    async def try_load_for_embedding(
        self, analysis_id: int
    ) -> ReadyForEmbedding | None: ...


class ReadyForEmbedding(BaseModel):
    """Stage 5 embedding を実行可能な状態を表す precondition 型 (厚い Ready)。

    フィールドは operation を特定する ID と、embedder 入力 text の全揃え。
    Ready が存在する = 処理開始時点で DB から text を取得済 + 行存在 + 未 embedded
    が verify された状態 (時間ずれゼロ)。Service は ``ready.text_for_embedding`` を
    直接 embedder に渡せばよく、自身で DB fetch / None チェックを行わない。

    Invariants:
    - ``analysis_id``: 正の整数 (DB の InScopeAssessment.id を指す)
    - ``text_for_embedding``: 結合済の embedder 入力 (translated_title + "\\n" +
      summary)。Repository が結合して返す
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    analysis_id: int = Field(gt=0)
    text_for_embedding: str = Field(min_length=1)

    @classmethod
    async def try_advance_from(
        cls,
        analysis_id: int,
        embedding_repo: EmbeddingPreconditionProtocol,
    ) -> ReadyForEmbedding | None:
        """analysis_id から Stage 5 へ advance できるかを判定する gatekeeper。

        Precondition (Stage 5 に進める条件):
        - 同 analysis_id の InScopeAssessment 行が存在
        - 同 analysis_id の Embedding 未生成
        - translated_title / summary が確定済 (Stage 4 の BC 境界が保証)

        本 method は Domain 層の named gateway として
        `Repository.try_load_for_embedding` にそのまま delegate する。
        Repository が atomic な 1 query で precondition 判定 + 厚い Ready の
        構築を完結させる。

        Returns:
            進める場合: `ReadyForEmbedding` (text 含む厚い型)
            進めない場合: `None` (業務正常状態、例外ではない)

        Args:
            analysis_id: 上流 Stage 4 で永続化された InScopeAssessment.id
            embedding_repo: ``try_load_for_embedding`` を備える Repository
        """
        return await embedding_repo.try_load_for_embedding(analysis_id)


class EmbeddingTrigger(BaseModel):
    """Stage 5 起動 trigger — kiq message 用の軽量 ID キャリア。

    precondition は保証せず ``analysis_id`` のみを運ぶ。下流 Stage 5 Task が
    ``ReadyForEmbedding.try_advance_from`` を呼んで処理開始時に最新の DB 状態から
    Ready を構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

    上流 (Stage 4 Task / maintenance backfill) は値 fetch を行わず本 Trigger に
    ID だけ詰めて kiq に enqueue する。これにより kiq message が軽量になり、
    かつ enqueue → 実行までの時間ずれの影響を受けない (Ready 構築時に最新の
    DB 状態を反映する)。

    taskiq formatter は Pydantic BaseModel(frozen=True) を要求するため
    ``BaseModel`` 派生 (memory `feedback_taskiq_basemodel_required.md`)。
    """

    model_config = ConfigDict(frozen=True)

    analysis_id: int = Field(gt=0)
