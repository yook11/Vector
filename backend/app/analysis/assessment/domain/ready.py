"""ReadyForAssessment — Stage 4 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §1.1 / §3.2 / §6.1 / §7 で確定した設計
の Assessment BC 実装。Stage 4 operation の前提条件 (Extraction 存在 +
InScopeAssessment 未生成 + OutOfScopeAssessment 未生成) を構造保証し、
``AssessmentService`` の precondition 分岐を消すために Stage 間 passport として
受け渡される。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
BaseModel(frozen=True) は Issue #558 で公式サポート。詳細は
memory `feedback_taskiq_basemodel_required.md`。

注 (PR3.5-d.0): field 名 / 型 / 順序は ``ReadyForClassification`` と完全一致で
維持する (taskiq の in-flight message 互換のため)。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.analysis.extraction.domain.extraction import Extraction


class InScopeExistenceProtocol(Protocol):
    """Stage 4 進行判定用 InScope Repository contract (cheap exists 判定)。"""

    async def exists_for_extraction(self, extraction_id: int) -> bool: ...


class OutOfScopeExistenceProtocol(Protocol):
    """Stage 4 進行判定用 OutOfScope Repository contract (cheap exists 判定)。"""

    async def exists_for_extraction(self, extraction_id: int) -> bool: ...


class ReadyForAssessment(BaseModel):
    """Stage 4 (Assessment) を実行可能な状態を表す precondition 型。

    フィールドは operation に必要な値だけ (extraction_id + classifier に渡す本文)。
    Aggregate Entity (Extraction) 全体は持たない (spec §1.1)。

    Invariants:
    - 全フィールドが Extraction の copy のため派生フィールド invariant は持たない
      (upstream の `Extraction.__post_init__` が保証済 — spec §6.2 / §6.3)
    - frozen: 生成後は不変 (Stage 間 passport として副作用なしに受け渡せる)
    """

    model_config = ConfigDict(frozen=True)

    extraction_id: int
    translated_title: str
    summary: str

    @classmethod
    async def try_advance_from(
        cls,
        extraction: Extraction,
        *,
        in_scope_repo: InScopeExistenceProtocol,
        out_of_scope_repo: OutOfScopeExistenceProtocol,
    ) -> ReadyForAssessment | None:
        """Extraction 完了から Stage 4 へ advance できるかを判定する gatekeeper。

        Precondition (Stage 4 に進める条件):
        - 同 extraction_id の InScopeAssessment 未生成
        - 同 extraction_id の OutOfScopeAssessment 未生成

        Returns:
            進める場合: `ReadyForAssessment`
            進めない場合: `None` (業務正常状態、例外ではない — spec §4.5 Failure mode 1)

        Args:
            extraction: 上流 Stage 3 で永続化された Extraction Entity
            in_scope_repo: cheap exists 判定可能な InScope Repository
            out_of_scope_repo: cheap exists 判定可能な OutOfScope Repository
        """
        if await in_scope_repo.exists_for_extraction(extraction.id):
            return None
        if await out_of_scope_repo.exists_for_extraction(extraction.id):
            return None
        return cls(
            extraction_id=extraction.id,
            translated_title=extraction.translated_title,
            summary=extraction.summary,
        )
