"""ReadyForEmbedding — Stage 5 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §1.1 / §3.2 / §6.1 / §7 で確定した設計
の embedding BC 実装。Stage 5 operation の前提条件 (Embedding 未生成) を構造保証し、
EmbeddingService の precondition 分岐を消すために Stage 間 passport として
受け渡される。

設計方針 (2026-05-11 更新): Ready 型は ID + 構造 precondition のみを passport
として運び、値 (translated_title / summary など) は DB を SSoT として
EmbeddingService で都度読む。AI 境界 ``InScope`` が永続化可能性を保証した時点で、
``in_scope_assessments`` 行は不変な snapshot として確定するため、Stage 5 が DB を
再 read しても同じ値が得られる (in-scope assessments は Extraction 再実行で更新
されない、`feedback_bc_boundary_guarantees_downstream`)。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
詳細は memory `feedback_taskiq_basemodel_required.md`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingExistenceProtocol(Protocol):
    """Stage E 進行判定用 Embedding Repository contract (cheap exists 判定)。"""

    async def is_embedded_for(self, analysis_id: int) -> bool: ...


class ReadyForEmbedding(BaseModel):
    """Stage 5 embedding を実行可能な状態を表す precondition 型。

    フィールドは operation を特定するための ID のみ。embedder 入力テキストは
    Stage 5 Service が ``in_scope_assessments`` 行から都度 fetch する
    (Ready の責務は ID + precondition の搬送に絞り、値は DB を SSoT とする)。

    Invariants:
    - ``analysis_id``: 正の整数 (DB の InScopeAssessment.id を指す)
    - frozen: 生成後は不変 (Stage 間 passport として副作用なしに受け渡せる)

    注 (PR3.5-d.0): field 名 ``analysis_id`` は taskiq in-flight message 互換と
    embedding stage rename の対象外につき据え置き。embedding 側 rename は
    別 PR で扱う。
    """

    model_config = ConfigDict(frozen=True)

    analysis_id: int = Field(gt=0)

    @classmethod
    async def try_advance_from(
        cls,
        analysis_id: int,
        embedding_repo: EmbeddingExistenceProtocol,
    ) -> ReadyForEmbedding | None:
        """analysis_id から Stage 5 へ advance できるかを判定する gatekeeper。

        Precondition (Stage 5 に進める条件):
        - 同 analysis_id の Embedding 未生成

        Returns:
            進める場合: `ReadyForEmbedding`
            進めない場合: `None` (業務正常状態、例外ではない — spec §4.5 Failure mode 1)

        Args:
            analysis_id: 上流 Stage 4 で永続化された InScopeAssessment.id
            embedding_repo: cheap exists 判定可能な Embedding Repository
        """
        if await embedding_repo.is_embedded_for(analysis_id):
            return None
        return cls(analysis_id=analysis_id)
