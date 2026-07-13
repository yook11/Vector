"""Question context contracts and output guards."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.agent.threads.contracts import ThreadMessageSnapshot

MAX_STANDALONE_QUESTION_LENGTH = 500
MAX_ANSWER_REQUIREMENT_LENGTH = 500
MAX_CONTENT_REQUIREMENTS = 8
MAX_RESPONSE_REQUIREMENTS = 4
MAX_RELEVANT_PRIOR_COVERAGE_LENGTH = 1500
MAX_ACTIVE_GOAL_LENGTH = 1000

CONTENT_REQUIREMENT_IDS = frozenset(
    f"c{index}" for index in range(1, MAX_CONTENT_REQUIREMENTS + 1)
)
RESPONSE_REQUIREMENT_IDS = frozenset(
    f"p{index}" for index in range(1, MAX_RESPONSE_REQUIREMENTS + 1)
)
ANSWER_REQUIREMENT_IDS = CONTENT_REQUIREMENT_IDS | RESPONSE_REQUIREMENT_IDS

StandaloneQuestion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_STANDALONE_QUESTION_LENGTH,
    ),
]
RelevantPriorCoverage = Annotated[
    str, StringConstraints(max_length=MAX_RELEVANT_PRIOR_COVERAGE_LENGTH)
]
ActiveGoal = Annotated[str, StringConstraints(max_length=MAX_ACTIVE_GOAL_LENGTH)]


class QuestionContextResponseInvalidError(ValueError):
    """Generator output cannot be consumed as a structured draft."""


class AnswerRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: str
    description: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_ANSWER_REQUIREMENT_LENGTH,
        ),
    ]

    @field_validator("requirement_id")
    @classmethod
    def _validate_requirement_id(cls, value: str) -> str:
        if value not in ANSWER_REQUIREMENT_IDS:
            raise ValueError("unknown answer requirement id")
        return value


class QuestionContext(BaseModel):
    """Validated question context passed into the agent core."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    standalone_question: StandaloneQuestion
    content_requirements: list[AnswerRequirement] = Field(
        default_factory=list,
        max_length=MAX_CONTENT_REQUIREMENTS,
    )
    response_requirements: list[AnswerRequirement] = Field(
        default_factory=list,
        max_length=MAX_RESPONSE_REQUIREMENTS,
    )
    relevant_prior_coverage: RelevantPriorCoverage = ""
    active_goal: ActiveGoal = ""

    @field_validator("content_requirements")
    @classmethod
    def _validate_content_requirement_ids(
        cls, values: list[AnswerRequirement]
    ) -> list[AnswerRequirement]:
        _validate_requirement_namespace(values, CONTENT_REQUIREMENT_IDS)
        return values

    @field_validator("response_requirements")
    @classmethod
    def _validate_response_requirement_ids(
        cls, values: list[AnswerRequirement]
    ) -> list[AnswerRequirement]:
        _validate_requirement_namespace(values, RESPONSE_REQUIREMENT_IDS)
        return values


class QuestionContextDraft(BaseModel):
    """Lenient structured output at the generator adapter boundary."""

    model_config = ConfigDict(frozen=True)

    standalone_question: str
    content_requirements: list[str] = Field(default_factory=list)
    response_requirements: list[str] = Field(default_factory=list)
    relevant_prior_coverage: str = ""
    active_goal: str = ""
    explicit_feedback_detected: bool = False


class QuestionContextTelemetry(BaseModel):
    explicit_feedback_detected: bool = False
    previous_answer_had_missing_aspects: bool = False


class QuestionContextPreparationResult(BaseModel):
    context: QuestionContext
    telemetry: QuestionContextTelemetry


class QuestionContextGenerator(Protocol):
    """LLM port that derives structured context from a bounded thread window."""

    async def generate(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> QuestionContextDraft: ...


def question_context_from_draft(draft: QuestionContextDraft) -> QuestionContext:
    """Normalize model text before applying the strict public context contract."""

    return QuestionContext(
        standalone_question=_clean(
            draft.standalone_question, MAX_STANDALONE_QUESTION_LENGTH
        ),
        content_requirements=_requirements_from_draft(
            draft.content_requirements,
            prefix="c",
            maximum_count=MAX_CONTENT_REQUIREMENTS,
        ),
        response_requirements=_requirements_from_draft(
            draft.response_requirements,
            prefix="p",
            maximum_count=MAX_RESPONSE_REQUIREMENTS,
        ),
        relevant_prior_coverage=_clean(
            draft.relevant_prior_coverage,
            MAX_RELEVANT_PRIOR_COVERAGE_LENGTH,
        ),
        active_goal=_clean(draft.active_goal, MAX_ACTIVE_GOAL_LENGTH),
    )


def _clean(value: str, maximum: int) -> str:
    return value.strip()[:maximum].strip()


def _validate_requirement_namespace(
    requirements: list[AnswerRequirement],
    allowed_ids: frozenset[str],
) -> None:
    if any(
        requirement.requirement_id not in allowed_ids for requirement in requirements
    ):
        raise ValueError("requirement id is not valid for this requirement list")
    if len({requirement.requirement_id for requirement in requirements}) != len(
        requirements
    ):
        raise ValueError("requirement ids must be unique within a requirement list")


def _requirements_from_draft(
    values: list[str],
    *,
    prefix: str,
    maximum_count: int,
) -> list[AnswerRequirement]:
    descriptions: list[str] = []
    for value in values:
        description = _clean(value, MAX_ANSWER_REQUIREMENT_LENGTH)
        if description and description not in descriptions:
            descriptions.append(description)
        if len(descriptions) == maximum_count:
            break
    return [
        AnswerRequirement(requirement_id=f"{prefix}{index}", description=description)
        for index, description in enumerate(descriptions, start=1)
    ]
