"""ReadyForEmbedding — Stage E 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §1.1 / §3.2 / §6.1 / §7 で確定した設計
の embedding BC 実装。Stage E operation の前提条件 (Analysis 存在 + Embedding
未生成) を構造保証し、EmbeddingService の precondition 分岐 (extraction_not_found /
analysis_pending / analysis_rejected / 既存 embedding) を消すために Stage 間
passport として受け渡される。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
詳細は memory `feedback_taskiq_basemodel_required.md`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.classification.domain.analysis import Analysis


class EmbeddingExistenceProtocol(Protocol):
    """Stage E 進行判定用 Embedding Repository contract (cheap exists 判定)。"""

    async def is_embedded_for(self, analysis_id: int) -> bool: ...


class ReadyForEmbedding(BaseModel):
    """Stage E embedding を実行可能な状態を表す precondition 型。

    フィールドは operation に必要な値だけ (analysis_id + embedder に渡す本文)。
    ``model_name`` は run-time に embedder.MODEL から決定される値で Ready の
    責務外 (= どの AI で処理するかは Ready が保証することではない)。

    Invariants:
    - ``analysis_id``: 正の整数 (DB の ArticleAnalysis.id を指す)
    - ``text_for_embedding``: 非空 (構築時 ``Field(min_length=1)`` で保証)
    - frozen: 生成後は不変 (Stage 間 passport として副作用なしに受け渡せる)
    """

    model_config = ConfigDict(frozen=True)

    analysis_id: int = Field(gt=0)
    text_for_embedding: str = Field(min_length=1)

    @classmethod
    async def try_advance_from(
        cls,
        analysis: Analysis,
        embedding_repo: EmbeddingExistenceProtocol,
    ) -> ReadyForEmbedding | None:
        """Classified 確定から Stage E へ advance できるかを判定する gatekeeper。

        Precondition (Stage E に進める条件):
        - 同 analysis_id の Embedding 未生成

        Returns:
            進める場合: `ReadyForEmbedding`
            進めない場合: `None` (業務正常状態、例外ではない — spec §4.5 Failure mode 1)

        Args:
            analysis: 上流 Stage D で永続化された Analysis Entity
            embedding_repo: cheap exists 判定可能な Embedding Repository
        """
        if await embedding_repo.is_embedded_for(analysis.id):
            return None
        return cls(
            analysis_id=analysis.id,
            text_for_embedding=f"{analysis.translated_title}\n{analysis.summary}",
        )
