"""Question context contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.question_context.contract import (
    MAX_PRIOR_COVERAGE_LENGTH,
    MAX_STANDALONE_QUESTION_LENGTH,
    MAX_USER_ACTIVITY_CONTEXT_LENGTH,
    MAX_USER_INTENT_LENGTH,
    QuestionContext,
    QuestionContextDraft,
    question_context_from_draft,
)


def test_draft_cleaning_strips_and_truncates_all_fields() -> None:
    context = question_context_from_draft(
        QuestionContextDraft(
            standalone_question="  q" * (MAX_STANDALONE_QUESTION_LENGTH + 1),
            user_intent=" i " * (MAX_USER_INTENT_LENGTH + 1),
            prior_coverage=" p " * (MAX_PRIOR_COVERAGE_LENGTH + 1),
            user_activity_context=" a " * (MAX_USER_ACTIVITY_CONTEXT_LENGTH + 1),
        )
    )

    assert len(context.standalone_question) <= MAX_STANDALONE_QUESTION_LENGTH
    assert len(context.user_intent) <= MAX_USER_INTENT_LENGTH
    assert len(context.prior_coverage) <= MAX_PRIOR_COVERAGE_LENGTH
    assert len(context.user_activity_context) <= MAX_USER_ACTIVITY_CONTEXT_LENGTH
    assert context.standalone_question == context.standalone_question.strip()
    assert context.user_intent == context.user_intent.strip()
    assert context.prior_coverage == context.prior_coverage.strip()
    assert context.user_activity_context == context.user_activity_context.strip()


def test_empty_optional_context_is_valid() -> None:
    context = QuestionContext(standalone_question="NVIDIA の直近発表は？")

    assert context.user_intent == ""
    assert context.prior_coverage == ""
    assert context.user_activity_context == ""


def test_blank_standalone_question_is_rejected_after_cleaning() -> None:
    with pytest.raises(ValidationError):
        question_context_from_draft(QuestionContextDraft(standalone_question=" \n "))


def test_question_context_keeps_max_length_as_final_guard() -> None:
    with pytest.raises(ValidationError):
        QuestionContext(standalone_question="x" * (MAX_STANDALONE_QUESTION_LENGTH + 1))
