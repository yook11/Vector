"""Question context contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent import question_context as question_context_package
from app.agent.question_context import contract


def test_question_context_from_draft_strips_and_caps_text_fields() -> None:
    context = contract.question_context_from_draft(
        contract.QuestionContextDraft(
            standalone_question=f"  {'q' * 501}  ",
            relevant_prior_coverage=f"  {'p' * 1501}  ",
            active_goal=f"  {'g' * 1001}  ",
        )
    )

    assert (
        context.standalone_question,
        context.relevant_prior_coverage,
        context.active_goal,
    ) == ("q" * 500, "p" * 1500, "g" * 1000)


def test_question_context_from_draft_normalizes_answer_requirements() -> None:
    """空白除去・500字cap・重複排除・8件capがanswer_requirementsで成立する。"""
    context = contract.question_context_from_draft(
        contract.QuestionContextDraft(
            standalone_question="半導体企業を比較して",
            answer_requirements=[
                "  Intel を含める  ",
                " \n ",
                "Intel を含める",
                "x" * 501,
                "x" * 500,
                "AMD を含める",
                "NVIDIA を含める",
                "TSMC を含める",
                "Samsung を含める",
                "Qualcomm を含める",
                "Broadcom を含める",
                "MediaTek を含める",
            ],
        )
    )

    assert context.answer_requirements == (
        "Intel を含める",
        "x" * 500,
        "AMD を含める",
        "NVIDIA を含める",
        "TSMC を含める",
        "Samsung を含める",
        "Qualcomm を含める",
        "Broadcom を含める",
    )


@pytest.mark.parametrize(
    "extra_field",
    (
        "user_intent",
        "prior_coverage",
        "user_activity_context",
        "actve_goal",
        "content_requirements",
        "response_requirements",
        "explicit_feedback_detected",
        "previous_answer_had_missing_aspects",
    ),
)
def test_question_context_direct_construction_rejects_unknown_fields(
    extra_field: str,
) -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(
            standalone_question="半導体企業を比較して",
            **{extra_field: "黙って受け入れてはいけない"},
        )


def test_question_context_rejects_more_than_max_answer_requirements() -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(
            standalone_question="半導体企業を比較して",
            answer_requirements=tuple(f"要望{index}" for index in range(9)),
        )


def test_question_context_direct_construction_rejects_blank_answer_requirement() -> (
    None
):
    with pytest.raises(ValidationError):
        contract.QuestionContext(
            standalone_question="半導体企業を比較して",
            answer_requirements=("",),
        )


def test_question_context_has_exactly_four_fields_with_empty_defaults() -> None:
    context = contract.QuestionContext(standalone_question="NVIDIA の直近発表は？")

    assert (
        set(contract.QuestionContext.model_fields),
        context.answer_requirements,
        context.relevant_prior_coverage,
        context.active_goal,
    ) == (
        {
            "standalone_question",
            "answer_requirements",
            "relevant_prior_coverage",
            "active_goal",
        },
        (),
        "",
        "",
    )


@pytest.mark.parametrize(
    "removed_symbol",
    (
        "AnswerRequirement",
        "QuestionContextTelemetry",
        "QuestionContextPreparationResult",
        "CONTENT_REQUIREMENT_IDS",
        "RESPONSE_REQUIREMENT_IDS",
        "ANSWER_REQUIREMENT_IDS",
        "MAX_RESPONSE_REQUIREMENTS",
    ),
)
def test_removed_symbols_are_absent_from_contract_module(removed_symbol: str) -> None:
    assert not hasattr(contract, removed_symbol)
    assert not hasattr(question_context_package, removed_symbol)


def test_question_context_rejects_blank_standalone_question_after_cleaning() -> None:
    with pytest.raises(ValidationError):
        contract.question_context_from_draft(
            contract.QuestionContextDraft(standalone_question=" \n ")
        )


def test_question_context_keeps_standalone_question_max_length_as_final_guard() -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(standalone_question="x" * 501)
