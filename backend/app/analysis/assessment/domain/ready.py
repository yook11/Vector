"""Stage 4 assessment を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AssessmentPreconditionProtocol",
    "AssessmentReadyBuildBlockedCode",
    "AssessmentReadyBuildBlockedError",
    "AssessmentReadyBuildFacts",
    "ReadyForAssessment",
]


class AssessmentReadyBuildBlockedCode(StrEnum):
    """Stage 4 Ready 構築 blocked の監査 outcome_code。"""

    CURATION_MISSING = "assessment_ready_build_blocked_curation_missing"
    ALREADY_IN_SCOPE = "assessment_ready_build_blocked_already_in_scope"
    ALREADY_OUT_OF_SCOPE = "assessment_ready_build_blocked_already_out_of_scope"


@dataclass(frozen=True, slots=True)
class AssessmentReadyBuildFacts:
    """Stage 4 Ready 構築に必要な DB 射影。"""

    curation_id: int
    analyzable_article_id: int
    translated_title: str
    summary: str
    has_analyzed_article: bool
    has_out_of_scope_article: bool


class AssessmentReadyBuildBlockedError(Exception):
    """Stage 4 入力として採用できなかった場合に投げる例外。"""

    def __init__(
        self,
        code: AssessmentReadyBuildBlockedCode,
        *,
        analyzable_article_id: int | None = None,
    ) -> None:
        self.code = code
        self.analyzable_article_id = analyzable_article_id
        super().__init__(code.value)


class AssessmentPreconditionProtocol(Protocol):
    """Ready 構築に必要な DB 事実だけを読む repository contract。

    構築可否と blocked 理由は ``ReadyForAssessment`` が判定する。
    """

    async def load_ready_build_facts(
        self, curation_id: int
    ) -> AssessmentReadyBuildFacts | None: ...


class ReadyForAssessment(BaseModel):
    """assessor 入力と Stage 4 precondition を満たした不変オブジェクト。"""

    model_config = ConfigDict(frozen=True)

    curation_id: int = Field(gt=0)
    translated_title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    analyzable_article_id: int = Field(gt=0)

    @classmethod
    async def try_advance_from(
        cls,
        *,
        curation_id: int,
        repo: AssessmentPreconditionProtocol,
    ) -> ReadyForAssessment:
        """DB 事実から Ready を構築し、対象外なら blocked 例外を投げる。"""
        facts = await repo.load_ready_build_facts(curation_id)
        if facts is None:
            raise AssessmentReadyBuildBlockedError(
                AssessmentReadyBuildBlockedCode.CURATION_MISSING
            )

        if facts.has_analyzed_article:
            raise AssessmentReadyBuildBlockedError(
                AssessmentReadyBuildBlockedCode.ALREADY_IN_SCOPE,
                analyzable_article_id=facts.analyzable_article_id,
            )

        if facts.has_out_of_scope_article:
            raise AssessmentReadyBuildBlockedError(
                AssessmentReadyBuildBlockedCode.ALREADY_OUT_OF_SCOPE,
                analyzable_article_id=facts.analyzable_article_id,
            )

        return cls(
            curation_id=facts.curation_id,
            translated_title=facts.translated_title,
            summary=facts.summary,
            analyzable_article_id=facts.analyzable_article_id,
        )
