"""Evidence Answer Agent declaration contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib import import_module, util
from types import SimpleNamespace
from typing import cast

from google.genai.client import AsyncClient

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerInput,
    RawEvidenceAnswerDraft,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.contract import ExternalUrlSource
from app.agent.planning.contract import TargetTimeWindow
from app.agent.question_context.contract import AnswerRequirement, QuestionContext
from app.agent.runtime.gemini import GeminiAgentRuntime
from tests.agent.runtime._helpers import FakeGeminiClient


class _SdkStream:
    def __init__(self) -> None:
        self._chunks = iter(
            [
                SimpleNamespace(
                    text="JSON_FRAGMENT_SENTINEL",
                    prompt_feedback=None,
                    candidates=[
                        SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))
                    ],
                    usage_metadata=None,
                )
            ]
        )
        self.close_calls = 0

    def __aiter__(self) -> _SdkStream:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_calls += 1


def _request(*, question: str = "QUESTION_CONTENTS_SENTINEL") -> AnsweringRequest:
    return AnsweringRequest(
        context=QuestionContext(
            standalone_question=question,
            content_requirements=[
                AnswerRequirement(
                    requirement_id="c1",
                    description="CONTENT_REQUIREMENT_SENTINEL",
                )
            ],
            response_requirements=[
                AnswerRequirement(
                    requirement_id="p1",
                    description="RESPONSE_REQUIREMENT_SENTINEL",
                )
            ],
            relevant_prior_coverage="PRIOR_COVERAGE_SENTINEL",
            active_goal="ACTIVE_GOAL_SENTINEL",
        ),
        as_of=datetime(2026, 7, 7, tzinfo=UTC),
    )


def _evidence() -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=ExternalUrlSource(
            source_ref="1",
            url="https://example.com/evidence",
            title="EVIDENCE_TITLE_SENTINEL",
            evidence_claim="EVIDENCE_CLAIM_SENTINEL",
        ),
        text="EVIDENCE_TEXT_SENTINEL",
    )


def _declaration() -> tuple[object, type[object]]:
    assert util.find_spec("app.agent.answering.evidence_answer.agent") is not None, (
        "Evidence Answer Agent declaration が未実装です"
    )
    contract = import_module("app.agent.answering.evidence_answer.contract")
    input_type = getattr(contract, "EvidenceAnswerInput", None)
    assert input_type is not None, "EvidenceAnswerInput が未実装です"
    agent_module = import_module("app.agent.answering.evidence_answer.agent")
    agent = getattr(agent_module, "EVIDENCE_ANSWER_AGENT", None)
    assert agent is not None, "EVIDENCE_ANSWER_AGENT が未実装です"
    return agent, input_type


def test_agent_declares_structured_gemini_role_and_manual_prompt_version() -> None:
    agent, _ = _declaration()

    assert (
        agent.name,
        agent.model.provider,
        agent.model.name,
        agent.model_settings.temperature,
        agent.model_settings.max_output_tokens,
        agent.prompt.version,
        agent.output_type,
    ) == (
        "evidence_answer",
        "gemini",
        "gemini-3.1-flash-lite",
        0.2,
        2048,
        "v2",
        RawEvidenceAnswerDraft,
    )
    assert agent.response_schema is not None


def test_fixed_instructions_and_rendered_input_are_separated() -> None:
    agent, input_type = _declaration()
    fixed = "ユーザーが知りたいことへ直接答えることです。"
    input = input_type(
        request=_request(),
        evidence=(_evidence(),),
        target_time_window=TargetTimeWindow(kind="last_n_days", days=7),
        previous_error="PREVIOUS_ERROR_SENTINEL",
    )

    rendered = agent.prompt.input_renderer(input)

    assert fixed in agent.prompt.instructions
    assert fixed not in rendered
    for sentinel in (
        "QUESTION_CONTENTS_SENTINEL",
        "CONTENT_REQUIREMENT_SENTINEL",
        "RESPONSE_REQUIREMENT_SENTINEL",
        "PRIOR_COVERAGE_SENTINEL",
        "ACTIVE_GOAL_SENTINEL",
        "EVIDENCE_TITLE_SENTINEL",
        "EVIDENCE_CLAIM_SENTINEL",
        "EVIDENCE_TEXT_SENTINEL",
        "PREVIOUS_ERROR_SENTINEL",
    ):
        assert sentinel in rendered
        assert sentinel not in agent.prompt.instructions
    assert "target_time_window: 直近7日" in rendered


async def test_runtime_request_keeps_fixed_and_dynamic_text_separate() -> None:
    sdk_stream = _SdkStream()
    client = FakeGeminiClient([], streams=[sdk_stream])
    input = EvidenceAnswerInput(
        request=_request(),
        evidence=(_evidence(),),
        target_time_window=TargetTimeWindow(kind="last_n_days", days=7),
        previous_error="PREVIOUS_ERROR_SENTINEL",
    )
    stream = GeminiAgentRuntime(client=cast(AsyncClient, client)).invoke_stream(
        EVIDENCE_ANSWER_AGENT,
        input,
        attempt_number=2,
    )

    fragments = [fragment async for fragment in cast(AsyncIterator[str], stream)]
    provider_request = client.models.generate_content_stream.await_args.kwargs
    provider_config = provider_request["config"]

    assert fragments == ["JSON_FRAGMENT_SENTINEL"]
    assert sdk_stream.close_calls == 1
    assert (
        provider_config.system_instruction == EVIDENCE_ANSWER_AGENT.prompt.instructions
    )
    assert provider_request["contents"] == EVIDENCE_ANSWER_AGENT.prompt.input_renderer(
        input
    )
    assert provider_config.response_mime_type == "application/json"
    assert provider_config.response_schema is not None
    assert "QUESTION_CONTENTS_SENTINEL" not in provider_config.system_instruction
    assert (
        "ユーザーが知りたいことへ直接答えることです。"
        not in provider_request["contents"]
    )


def test_response_schema_matches_lenient_raw_draft_representative_payload() -> None:
    agent, _ = _declaration()
    schema = agent.response_schema
    assert schema is not None
    assert set(schema["required"]) == set(RawEvidenceAnswerDraft.model_fields)
    assert set(schema["properties"]) == set(RawEvidenceAnswerDraft.model_fields)

    draft = agent.output_type.model_validate(
        {
            "sufficiency": "answered",
            "answer": "回答です。[[1]]",
            "cited_refs": ["1"],
            "missing_aspects": [],
            "unfulfilled_requirement_ids": [],
        }
    )

    assert draft == RawEvidenceAnswerDraft(
        sufficiency="answered",
        answer="回答です。[[1]]",
        cited_refs=["1"],
    )
