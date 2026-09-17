"""Stage 4 assessment を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "AssessmentReadyBuildRejectionReason",
    "AssessmentReadyBuildRejected",
    "AssessmentReadyBuildFacts",
    "ReadyForAssessment",
]


class AssessmentReadyBuildRejectionReason(StrEnum):
    """Ready構築を拒否する理由と既存の監査コード。"""

    CURATION_MISSING = "assessment_ready_build_blocked_curation_missing"
    INPUT_INVALID = "assessment_ready_build_blocked_input_invalid"
    ALREADY_IN_SCOPE = "assessment_ready_build_blocked_already_in_scope"
    ALREADY_OUT_OF_SCOPE = "assessment_ready_build_blocked_already_out_of_scope"

    @property
    def is_idempotent_skip(self) -> bool:
        """別 worker が先に処理済みで no-op になった冪等 skip か (勝者の行と冗長)。"""
        return self in {
            AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
            AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE,
        }


@dataclass(frozen=True, slots=True)
class AssessmentReadyBuildFacts:
    """Stage 4 Ready 構築に必要な DB 射影。"""

    curation_id: int
    analyzable_article_id: int
    translated_title: str
    summary: str
    has_analyzed_article: bool
    has_out_of_scope_article: bool


@dataclass(frozen=True, slots=True)
class AssessmentReadyBuildRejected:
    """Readyを構築できない理由とDBで確認した記事IDを表す。"""

    reason: AssessmentReadyBuildRejectionReason
    analyzable_article_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, AssessmentReadyBuildRejectionReason):
            raise TypeError("reason must be AssessmentReadyBuildRejectionReason")


class ReadyForAssessment(BaseModel):
    """assessor 入力と Stage 4 precondition を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    curation_id: int = Field(gt=0)
    translated_title: str = Field(min_length=1)
    summary: str = Field(min_length=1)

    @classmethod
    def from_facts(
        cls,
        curation_id: int,
        facts: AssessmentReadyBuildFacts | None,
    ) -> tuple[ReadyForAssessment, int] | AssessmentReadyBuildRejected:
        """取得済みの事実から、I/Oなしで開始条件と入力を検証する。"""
        if facts is None:
            return AssessmentReadyBuildRejected(
                AssessmentReadyBuildRejectionReason.CURATION_MISSING
            )

        if facts.has_analyzed_article:
            return AssessmentReadyBuildRejected(
                AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
                analyzable_article_id=facts.analyzable_article_id,
            )

        if facts.has_out_of_scope_article:
            return AssessmentReadyBuildRejected(
                AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE,
                analyzable_article_id=facts.analyzable_article_id,
            )

        try:
            ready = cls(
                curation_id=facts.curation_id,
                translated_title=facts.translated_title,
                summary=facts.summary,
            )
        except ValidationError:
            return AssessmentReadyBuildRejected(
                AssessmentReadyBuildRejectionReason.INPUT_INVALID,
                analyzable_article_id=facts.analyzable_article_id,
            )
        return ready, facts.analyzable_article_id
