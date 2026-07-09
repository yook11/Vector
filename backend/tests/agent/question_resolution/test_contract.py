"""Question resolution contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.question_resolution.contract import (
    MAX_PRIOR_COVERAGE_LENGTH,
    MAX_STANDALONE_QUESTION_LENGTH,
    MAX_USER_ACTIVITY_CONTEXT_LENGTH,
    MAX_USER_INTENT_LENGTH,
    ResolvedQuestion,
    ResolvedQuestionDraft,
    resolved_question_from_draft,
)


def test_draft_cleaning_strips_and_truncates_all_fields() -> None:
    resolved = resolved_question_from_draft(
        ResolvedQuestionDraft(
            standalone_question="  q" * (MAX_STANDALONE_QUESTION_LENGTH + 1),
            user_intent=" i " * (MAX_USER_INTENT_LENGTH + 1),
            prior_coverage=" p " * (MAX_PRIOR_COVERAGE_LENGTH + 1),
            user_activity_context=" a " * (MAX_USER_ACTIVITY_CONTEXT_LENGTH + 1),
        )
    )

    assert len(resolved.standalone_question) <= MAX_STANDALONE_QUESTION_LENGTH
    assert len(resolved.user_intent) <= MAX_USER_INTENT_LENGTH
    assert len(resolved.prior_coverage) <= MAX_PRIOR_COVERAGE_LENGTH
    assert len(resolved.user_activity_context) <= MAX_USER_ACTIVITY_CONTEXT_LENGTH
    assert resolved.standalone_question == resolved.standalone_question.strip()
    assert resolved.user_intent == resolved.user_intent.strip()
    assert resolved.prior_coverage == resolved.prior_coverage.strip()
    assert resolved.user_activity_context == resolved.user_activity_context.strip()


def test_empty_optional_context_is_valid() -> None:
    resolved = ResolvedQuestion(standalone_question="NVIDIA の直近発表は？")

    assert resolved.user_intent == ""
    assert resolved.prior_coverage == ""
    assert resolved.user_activity_context == ""


def test_blank_standalone_question_is_rejected_after_cleaning() -> None:
    with pytest.raises(ValidationError):
        resolved_question_from_draft(ResolvedQuestionDraft(standalone_question=" \n "))


def test_resolved_question_keeps_max_length_as_final_guard() -> None:
    with pytest.raises(ValidationError):
        ResolvedQuestion(standalone_question="x" * (MAX_STANDALONE_QUESTION_LENGTH + 1))
