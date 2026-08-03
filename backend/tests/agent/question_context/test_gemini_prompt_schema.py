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
    """v3: フィールド定義はschema descriptionへ移り、instructionsは

    フィールド横断の共通規則(missing_aspectsの反映条件・新topicでの空化・
    履歴にない事実の補完禁止)だけを持つ(spec: 「question_context prompt
    (v2 -> v3)」節)。
    """
    assert "answer_requirements" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "active_goal" in QUESTION_CONTEXT_INSTRUCTIONS
    assert "response schema の description に従ってください" in (
        QUESTION_CONTEXT_INSTRUCTIONS
    )
    assert "新topicではactive_goalとrelevant_prior_coverageを空にする" in (
        QUESTION_CONTEXT_INSTRUCTIONS
    )
    assert "履歴にない事実、要望、目的を補完・推測しない" in (
        QUESTION_CONTEXT_INSTRUCTIONS
    )
    assert not any(
        old_wording in QUESTION_CONTEXT_INSTRUCTIONS
        for old_wording in (
            "content_requirements",
            "response_requirements",
            "explicit_feedback_detected",
            "retrieval mode",
            "検索query",
            "検索provider",
            "source再利用可否",
        )
    )


def test_instructions_describe_missing_aspects_as_prior_answer_gaps() -> None:
    """missing_aspectsは「満たせなかった要望」ではなく

    「前回の回答で確認できなかったこと」として説明される(spec:
    agent-answer-self-report-removal-slice.md 「精査の不足はagentへの入力として
    渡す」節 / requirement単位で追跡する機構は設けない)。
    """
    assert "満たせなかった保存済みの要望" not in QUESTION_CONTEXT_INSTRUCTIONS
    assert "対応するrequirementへ昇格する" not in QUESTION_CONTEXT_INSTRUCTIONS
    assert (
        "assistant messageのmissing_aspectsは、前回の回答で確認できなかったことである。"
        in QUESTION_CONTEXT_INSTRUCTIONS
    )


def test_schema_and_agent_require_every_question_context_draft_field() -> None:
    expected_fields = set(QuestionContextDraft.model_fields)
    declared_schema = thaw_schema(QUESTION_CONTEXT_AGENT.response_schema)

    assert expected_fields == {
        "standalone_question",
        "answer_requirements",
        "relevant_prior_coverage",
        "active_goal",
    }
    assert set(QUESTION_CONTEXT_GEMINI_SCHEMA["required"]) == expected_fields
    assert set(QUESTION_CONTEXT_GEMINI_SCHEMA["properties"]) == expected_fields
    assert declared_schema == QUESTION_CONTEXT_GEMINI_SCHEMA


def test_schema_field_descriptions_match_the_confirmed_definitions() -> None:
    """schema descriptionが仕様の確定文言と一致し、旧語彙が残存しない

    (spec Test contract: 「question_context / planner の schema description が
    仕様の確定文言と一致し、旧語彙("for retrieval" / "to avoid repeating" /
    "research flow" / "Null means")が残存しない」)。
    """
    properties = QUESTION_CONTEXT_GEMINI_SCHEMA["properties"]

    assert properties["standalone_question"]["description"] == (
        "履歴を知らなくても意味が通る形にした現在の質問。"
        "自己完結していればほぼそのまま返す。代名詞・省略は履歴に根拠がある"
        "対象だけを補う。"
    )
    assert properties["answer_requirements"]["description"] == (
        "回答が満たすべき条件。ユーザーが明示・含意した要求だけを分解する。"
        "調査観点や比較軸を発明しない。"
    )
    assert properties["active_goal"]["description"] == (
        "履歴または現在の質問に明確な根拠がある、スレッド全体の作業・調査の"
        "目的。無ければ空文字。"
    )
    assert properties["relevant_prior_coverage"]["description"] == (
        "今回の質問に関係する既回答の簡潔な要約。無ければ空文字。"
    )
    schema_text = "".join(field.get("description", "") for field in properties.values())
    assert not any(
        old_wording in schema_text
        for old_wording in (
            "for retrieval",
            "to avoid repeating",
            "research flow",
            "Null means",
        )
    )


def test_representative_schema_payload_matches_python_output_contract() -> None:
    payload = {
        "standalone_question": "NVIDIA の直近発表は？",
        "answer_requirements": ["発表内容を含める"],
        "relevant_prior_coverage": "前回は製品概要を説明済み",
        "active_goal": "半導体投資を調査する",
    }

    assert QuestionContextDraft.model_validate(payload).model_dump() == payload
