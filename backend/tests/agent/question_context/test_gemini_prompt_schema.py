"""Question Context Agent prompt/schema tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.question_context.ai.schema_tool import QUESTION_CONTEXT_GEMINI_SCHEMA
from app.agent.question_context.contract import (
    QuestionContextDraft,
    QuestionContextGenerationInput,
)
from app.agent.question_context.prompts import (
    QUESTION_CONTEXT_INSTRUCTIONS,
    render_question_context_input,
)
from app.agent.runtime._structured_output import thaw_schema
from app.agent.threads.contracts import ThreadMessageSnapshot


def _input() -> QuestionContextGenerationInput:
    return QuestionContextGenerationInput(
        question="前回の比較を更新して",
        history=(
            ThreadMessageSnapshot(
                role="assistant",
                content="最初の回答",
                missing_aspects=("最初の保存不足",),
            ),
            ThreadMessageSnapshot(role="user", content="次の質問"),
        ),
        as_of=datetime(2026, 7, 10, tzinfo=UTC),
    )


def test_renderer_keeps_missing_aspects_with_the_assistant_message() -> None:
    rendered = render_question_context_input(_input())
    assistant_start = rendered.index("role: assistant")
    user_start = rendered.index("role: user")
    assistant = rendered[assistant_start:user_start]

    assert "<untrusted_input>" in assistant
    assert "最初の回答" in assistant
    assert "missing_aspects:" in assistant
    assert "最初の保存不足" in assistant
    assert "</untrusted_input>" in assistant


def test_instructions_define_context_preparation_rules() -> None:
    assert "content_requirements" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "何を答えるか" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "response_requirements" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "どう答えるか" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "Intelが抜けている" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "表にしてと言った" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "生のfeedback本文を完成contextへ残さない" in (QUESTION_CONTEXT_INSTRUCTIONS)
    assert "retrieval mode" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "検索query" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "検索provider" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "source再利用可否" in QUESTION_CONTEXT_INSTRUCTIONS


def test_schema_and_agent_require_every_question_context_draft_field() -> None:
    expected_fields = set(QuestionContextDraft.model_fields)
    declared_schema = thaw_schema(QUESTION_CONTEXT_AGENT.response_schema)

    assert set(QUESTION_CONTEXT_GEMINI_SCHEMA["required"]) == expected_fields
    assert set(QUESTION_CONTEXT_GEMINI_SCHEMA["properties"]) == expected_fields
    assert declared_schema == QUESTION_CONTEXT_GEMINI_SCHEMA


def test_representative_schema_payload_matches_python_output_contract() -> None:
    payload = {
        "standalone_question": "NVIDIA の直近発表は？",
        "content_requirements": ["発表内容を含める"],
        "response_requirements": ["簡潔に説明する"],
        "relevant_prior_coverage": "前回は製品概要を説明済み",
        "active_goal": "半導体投資を調査する",
        "explicit_feedback_detected": False,
    }

    assert QuestionContextDraft.model_validate(payload).model_dump() == payload
