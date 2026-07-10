"""Question-resolution contracts and output guards."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.agent.conversations.contracts import ThreadMessageSnapshot

MAX_STANDALONE_QUESTION_LENGTH = 500
MAX_USER_INTENT_LENGTH = 500
MAX_PRIOR_COVERAGE_LENGTH = 1500
MAX_USER_ACTIVITY_CONTEXT_LENGTH = 1000

StandaloneQuestion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_STANDALONE_QUESTION_LENGTH,
    ),
]
UserIntent = Annotated[str, StringConstraints(max_length=MAX_USER_INTENT_LENGTH)]
PriorCoverage = Annotated[str, StringConstraints(max_length=MAX_PRIOR_COVERAGE_LENGTH)]
UserActivityContext = Annotated[
    str, StringConstraints(max_length=MAX_USER_ACTIVITY_CONTEXT_LENGTH)
]


class QuestionResolutionResponseInvalidError(ValueError):
    """Resolver output cannot be consumed as a structured draft."""


class ResolvedQuestion(BaseModel):
    """Validated question context passed into the agent core."""

    model_config = ConfigDict(frozen=True)

    standalone_question: StandaloneQuestion
    user_intent: UserIntent = ""
    prior_coverage: PriorCoverage = ""
    user_activity_context: UserActivityContext = ""


class ResolvedQuestionDraft(BaseModel):
    """Lenient structured output at the resolver adapter boundary."""

    model_config = ConfigDict(frozen=True)

    standalone_question: str
    user_intent: str = ""
    prior_coverage: str = ""
    user_activity_context: str = ""


class QuestionResolver(Protocol):
    """LLM port that derives structured context from a bounded thread window."""

    async def resolve(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> ResolvedQuestionDraft: ...


def resolved_question_from_draft(draft: ResolvedQuestionDraft) -> ResolvedQuestion:
    """Normalize model text before applying the strict public context contract."""

    return ResolvedQuestion(
        standalone_question=_clean(
            draft.standalone_question, MAX_STANDALONE_QUESTION_LENGTH
        ),
        user_intent=_clean(draft.user_intent, MAX_USER_INTENT_LENGTH),
        prior_coverage=_clean(draft.prior_coverage, MAX_PRIOR_COVERAGE_LENGTH),
        user_activity_context=_clean(
            draft.user_activity_context, MAX_USER_ACTIVITY_CONTEXT_LENGTH
        ),
    )


def _clean(value: str, maximum: int) -> str:
    return value.strip()[:maximum].strip()
