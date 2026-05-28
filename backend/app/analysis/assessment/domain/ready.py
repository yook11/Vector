"""Stage 4 assessment を開始できる状態を Domain 側で構築する。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AssessmentPreconditionProtocol",
    "AssessmentReadyBuildBlocked",
    "AssessmentReadyBuildBlockedCode",
    "AssessmentReadyBuildBlockedError",
    "AssessmentReadyBuildFacts",
    "ReadyForAssessment",
]


class AssessmentReadyBuildBlockedCode(StrEnum):
    """Stage 4 Ready 構築が業務状態により進めなかった理由。"""

    CURATION_MISSING = "curation_missing"
    ALREADY_IN_SCOPE = "already_in_scope"
    ALREADY_OUT_OF_SCOPE = "already_out_of_scope"


@dataclass(frozen=True, slots=True)
class AssessmentReadyBuildBlocked:
    """Stage 4 Ready 構築が正常に判定され、対象外だった結果。"""

    curation_id: int
    code: AssessmentReadyBuildBlockedCode
    article_id: int | None = None
    source_name: str | None = None


@dataclass(frozen=True, slots=True)
class AssessmentReadyBuildFacts:
    """Stage 4 Ready 構築に必要な DB 射影。"""

    curation_id: int
    article_id: int
    translated_title: str
    summary: str
    source_name: str | None
    has_in_scope_assessment: bool
    has_out_of_scope_assessment: bool


class AssessmentReadyBuildBlockedError(Exception):
    """Stage 4 Ready 構築が業務状態により進めなかったことを表す例外。"""

    def __init__(self, blocked: AssessmentReadyBuildBlocked) -> None:
        self.blocked = blocked
        super().__init__(blocked.code.value)


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
    article_id: int = Field(gt=0)
    source_name: str | None = None

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
                AssessmentReadyBuildBlocked(
                    curation_id=curation_id,
                    code=AssessmentReadyBuildBlockedCode.CURATION_MISSING,
                )
            )

        if facts.has_in_scope_assessment:
            raise AssessmentReadyBuildBlockedError(
                AssessmentReadyBuildBlocked(
                    curation_id=curation_id,
                    code=AssessmentReadyBuildBlockedCode.ALREADY_IN_SCOPE,
                    article_id=facts.article_id,
                    source_name=facts.source_name,
                )
            )

        if facts.has_out_of_scope_assessment:
            raise AssessmentReadyBuildBlockedError(
                AssessmentReadyBuildBlocked(
                    curation_id=curation_id,
                    code=AssessmentReadyBuildBlockedCode.ALREADY_OUT_OF_SCOPE,
                    article_id=facts.article_id,
                    source_name=facts.source_name,
                )
            )

        return cls(
            curation_id=facts.curation_id,
            translated_title=facts.translated_title,
            summary=facts.summary,
            article_id=facts.article_id,
            source_name=facts.source_name,
        )
