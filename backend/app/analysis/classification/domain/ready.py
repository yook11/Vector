"""ReadyForClassification — Stage D 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §1.1 / §3.2 / §6.1 / §7 で確定した設計
の classification BC 実装。Stage D operation の前提条件 (Extraction 存在 +
Analysis 未生成 + Rejection 未生成) を構造保証し、ClassificationService の
precondition 分岐を消すために Stage 間 passport として受け渡される。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
BaseModel(frozen=True) は Issue #558 で公式サポート。詳細は
memory `feedback_taskiq_basemodel_required.md`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.analysis.extraction.domain.extraction import Extraction


class AnalysisExistenceProtocol(Protocol):
    """Stage D 進行判定用 Analysis Repository contract (cheap exists 判定)。"""

    async def exists_for_extraction(self, extraction_id: int) -> bool: ...


class RejectionExistenceProtocol(Protocol):
    """Stage D 進行判定用 Rejection Repository contract (cheap exists 判定)。"""

    async def exists_for_extraction(self, extraction_id: int) -> bool: ...


class ReadyForClassification(BaseModel):
    """Stage D classification を実行可能な状態を表す precondition 型。

    フィールドは operation に必要な値だけ (extraction_id + classifier に渡す本文)。
    Aggregate Entity (Extraction) 全体は持たない (spec §1.1)。

    Invariants:
    - 全フィールドが Extraction の copy のため派生フィールド invariant は持たない
      (upstream の `Extraction.__post_init__` が保証済 — spec §6.2 / §6.3)
    - frozen: 生成後は不変 (Stage 間 passport として副作用なしに受け渡せる)

    `article_id` は Phase 1 transitional フィールド: classify_content task 後の
    `generate_embedding.kiq(article_id)` chain に必要 (Phase 2 で `ReadyForEmbedding`
    導入後に削除予定)。
    """

    model_config = ConfigDict(frozen=True)

    article_id: int
    extraction_id: int
    translated_title: str
    summary: str

    @classmethod
    async def try_advance_from(
        cls,
        extraction: Extraction,
        *,
        article_id: int,
        analysis_repo: AnalysisExistenceProtocol,
        rejection_repo: RejectionExistenceProtocol,
    ) -> ReadyForClassification | None:
        """Extraction 完了から Stage D へ advance できるかを判定する gatekeeper。

        Precondition (Stage D に進める条件):
        - 同 extraction_id の Analysis 未生成
        - 同 extraction_id の Rejection 未生成

        Returns:
            進める場合: `ReadyForClassification`
            進めない場合: `None` (業務正常状態、例外ではない — spec §4.5 Failure mode 1)

        Args:
            extraction: 上流 Stage C で永続化された Extraction Entity
            article_id: Phase 1 chain 用の article 識別子 (Phase 2 で削除予定)
            analysis_repo: cheap exists 判定可能な Analysis Repository
            rejection_repo: cheap exists 判定可能な Rejection Repository
        """
        if await analysis_repo.exists_for_extraction(extraction.id):
            return None
        if await rejection_repo.exists_for_extraction(extraction.id):
            return None
        return cls(
            article_id=article_id,
            extraction_id=extraction.id,
            translated_title=extraction.translated_title,
            summary=extraction.summary,
        )
