"""Question context contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_question_context_from_draft_normalizes_content_requirements() -> None:
    context = contract.question_context_from_draft(
        contract.QuestionContextDraft(
            standalone_question="半導体企業を比較して",
            content_requirements=[
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

    assert (
        [
            (requirement.requirement_id, requirement.description)
            for requirement in context.content_requirements
        ],
        context.response_requirements,
    ) == (
        [
            ("c1", "Intel を含める"),
            ("c2", "x" * 500),
            ("c3", "AMD を含める"),
            ("c4", "NVIDIA を含める"),
            ("c5", "TSMC を含める"),
            ("c6", "Samsung を含める"),
            ("c7", "Qualcomm を含める"),
            ("c8", "Broadcom を含める"),
        ],
        [],
    )


def test_question_context_from_draft_normalizes_response_requirements() -> None:
    context = contract.question_context_from_draft(
        contract.QuestionContextDraft(
            standalone_question="半導体企業を比較して",
            content_requirements=["Intel を含める"],
            response_requirements=[
                "  表形式で回答する  ",
                "\n",
                "表形式で回答する",
                "x" * 501,
                "x" * 500,
                "専門家向けにする",
                "簡潔にする",
                "この要望は上限を超える",
            ],
        )
    )

    assert (
        [
            (requirement.requirement_id, requirement.description)
            for requirement in context.content_requirements
        ],
        [
            (requirement.requirement_id, requirement.description)
            for requirement in context.response_requirements
        ],
    ) == (
        [("c1", "Intel を含める")],
        [
            ("p1", "表形式で回答する"),
            ("p2", "x" * 500),
            ("p3", "専門家向けにする"),
            ("p4", "簡潔にする"),
        ],
    )


def test_question_context_direct_construction_accepts_own_requirement_namespaces() -> (
    None
):
    context = contract.QuestionContext(
        standalone_question="半導体企業を比較して",
        content_requirements=[
            contract.AnswerRequirement(
                requirement_id="c1",
                description="Intel を含める",
            )
        ],
        response_requirements=[
            contract.AnswerRequirement(
                requirement_id="p1",
                description="表形式で回答する",
            )
        ],
    )

    assert (
        [
            (requirement.requirement_id, requirement.description)
            for requirement in context.content_requirements
        ],
        [
            (requirement.requirement_id, requirement.description)
            for requirement in context.response_requirements
        ],
    ) == ([("c1", "Intel を含める")], [("p1", "表形式で回答する")])


@pytest.mark.parametrize(
    "extra_field",
    ("user_intent", "prior_coverage", "user_activity_context", "actve_goal"),
)
def test_question_context_direct_construction_rejects_unknown_fields(
    extra_field: str,
) -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(
            standalone_question="半導体企業を比較して",
            **{extra_field: "黙って受け入れてはいけない"},
        )


@pytest.mark.parametrize(
    ("field_name", "wrong_requirement_id"),
    (("content_requirements", "p1"), ("response_requirements", "c1")),
)
def test_question_context_direct_construction_rejects_wrong_requirement_namespace(
    field_name: str,
    wrong_requirement_id: str,
) -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(
            standalone_question="半導体企業を比較して",
            **{
                field_name: [
                    contract.AnswerRequirement(
                        requirement_id=wrong_requirement_id,
                        description="namespace が不正な要望",
                    )
                ]
            },
        )


@pytest.mark.parametrize(
    ("field_name", "requirement_id"),
    (("content_requirements", "c1"), ("response_requirements", "p1")),
)
def test_question_context_direct_construction_rejects_duplicate_requirement_ids(
    field_name: str,
    requirement_id: str,
) -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(
            standalone_question="半導体企業を比較して",
            **{
                field_name: [
                    contract.AnswerRequirement(
                        requirement_id=requirement_id,
                        description="最初の要望",
                    ),
                    contract.AnswerRequirement(
                        requirement_id=requirement_id,
                        description="同じIDの二つ目の要望",
                    ),
                ]
            },
        )


def test_empty_coverage_goal_and_default_telemetry_are_valid() -> None:
    context = contract.QuestionContext(standalone_question="NVIDIA の直近発表は？")
    telemetry = contract.QuestionContextTelemetry()

    assert (
        set(contract.QuestionContext.model_fields),
        context.content_requirements,
        context.response_requirements,
        context.relevant_prior_coverage,
        context.active_goal,
        telemetry.model_dump(),
    ) == (
        {
            "standalone_question",
            "content_requirements",
            "response_requirements",
            "relevant_prior_coverage",
            "active_goal",
        },
        [],
        [],
        "",
        "",
        {
            "explicit_feedback_detected": False,
            "previous_answer_had_missing_aspects": False,
        },
    )


def test_question_context_rejects_blank_standalone_question_after_cleaning() -> None:
    with pytest.raises(ValidationError):
        contract.question_context_from_draft(
            contract.QuestionContextDraft(standalone_question=" \n ")
        )


def test_question_context_keeps_standalone_question_max_length_as_final_guard() -> None:
    with pytest.raises(ValidationError):
        contract.QuestionContext(standalone_question="x" * 501)
