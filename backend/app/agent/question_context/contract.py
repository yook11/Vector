"""Answer brief contracts and output guards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.agent.threads.contracts import ThreadMessageSnapshot

MAX_STANDALONE_QUESTION_LENGTH = 500
MAX_ANSWER_REQUIREMENTS = 8
MAX_ANSWER_REQUIREMENT_LENGTH = 500
MAX_RELEVANT_PRIOR_COVERAGE_LENGTH = 1500
MAX_ACTIVE_GOAL_LENGTH = 1000

StandaloneQuestion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_STANDALONE_QUESTION_LENGTH,
    ),
]
AnswerRequirementText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_ANSWER_REQUIREMENT_LENGTH,
    ),
]
RelevantPriorCoverage = Annotated[
    str, StringConstraints(max_length=MAX_RELEVANT_PRIOR_COVERAGE_LENGTH)
]
ActiveGoal = Annotated[str, StringConstraints(max_length=MAX_ACTIVE_GOAL_LENGTH)]


class AnswerBrief(BaseModel):
    """後段モデルへ渡す、質問の整理済みの伝え方。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    standalone_question: StandaloneQuestion
    answer_requirements: tuple[AnswerRequirementText, ...] = Field(
        default_factory=tuple,
        max_length=MAX_ANSWER_REQUIREMENTS,
    )
    relevant_prior_coverage: RelevantPriorCoverage = ""
    active_goal: ActiveGoal = ""


class AnswerBriefDraft(BaseModel):
    """Lenient structured output at the generator adapter boundary."""

    model_config = ConfigDict(frozen=True)

    standalone_question: str
    answer_requirements: list[str] = Field(default_factory=list)
    relevant_prior_coverage: str = ""
    active_goal: str = ""


@dataclass(frozen=True, slots=True)
class QuestionContextGenerationInput:
    """Question Context Agentへ渡す、Service投影済みの入力。"""

    question: str
    history: tuple[ThreadMessageSnapshot, ...]
    as_of: datetime


def answer_brief_from_draft(draft: AnswerBriefDraft) -> AnswerBrief:
    """Normalize model text before applying the strict public brief contract."""

    return AnswerBrief(
        standalone_question=_clean(
            draft.standalone_question, MAX_STANDALONE_QUESTION_LENGTH
        ),
        answer_requirements=_answer_requirements_from_draft(draft.answer_requirements),
        relevant_prior_coverage=_clean(
            draft.relevant_prior_coverage,
            MAX_RELEVANT_PRIOR_COVERAGE_LENGTH,
        ),
        active_goal=_clean(draft.active_goal, MAX_ACTIVE_GOAL_LENGTH),
    )


def _clean(value: str, maximum: int) -> str:
    return value.strip()[:maximum].strip()


def _answer_requirements_from_draft(values: list[str]) -> tuple[str, ...]:
    descriptions: list[str] = []
    for value in values:
        description = _clean(value, MAX_ANSWER_REQUIREMENT_LENGTH)
        if description and description not in descriptions:
            descriptions.append(description)
        if len(descriptions) == MAX_ANSWER_REQUIREMENTS:
            break
    return tuple(descriptions)
