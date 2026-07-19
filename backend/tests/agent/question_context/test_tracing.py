"""Question Context phaseとprovider attemptのproduction trace契約。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import logfire
import pytest
from google.genai.client import AsyncClient
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.question_context.service import QuestionContextService
from app.agent.runtime.contract import AgentRuntime
from app.agent.runtime.gemini import GeminiAgentRuntime
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.logfire.redaction import install_exception_redaction
from tests.agent.runtime._helpers import FakeGeminiClient, FakeResponse
from tests.logfire._span_helpers import (
    domain_attr_keys,
    exception_event,
    one_span_named,
    spans_named,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000020")
_RUN_SPAN_NAME = "agent_answering_run"
_PHASE_SPAN_NAME = "agent_phase"
_PROVIDER_SPAN_NAME = "agent_provider_call"


def _successful_response() -> FakeResponse:
    return FakeResponse(
        text=json.dumps(
            {
                "standalone_question": "NVIDIA の直近発表は？",
                "content_requirements": ["発表内容"],
                "response_requirements": [],
                "relevant_prior_coverage": "",
                "active_goal": "",
                "explicit_feedback_detected": False,
            }
        ),
        usage_metadata=SimpleNamespace(
            prompt_token_count=13,
            candidates_token_count=5,
        ),
    )


def _service(client: FakeGeminiClient) -> QuestionContextService:
    runtime = GeminiAgentRuntime(client=cast(AsyncClient, client))

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[AgentRuntime]:
        yield runtime

    return QuestionContextService(
        agent=QUESTION_CONTEXT_AGENT,
        runtime_scope_factory=runtime_scope,
    )


async def _prepare(service: QuestionContextService, *, question: str) -> object:
    return await service.prepare(
        question=question,
        history=(
            [
                ThreadMessageSnapshot(
                    role="assistant",
                    content="HISTORY_SENTINEL_7ac1",
                    missing_aspects=("MISSING_SENTINEL_c31f",),
                )
            ]
        ),
        as_of=datetime(2026, 7, 10, tzinfo=UTC),
        run_id=_RUN_ID,
    )


async def test_success_keeps_phase_and_attempt_in_the_answering_trace(
    capfire: CaptureLogfire,
) -> None:
    question = "QUESTION_SENTINEL_1c6d"
    client = FakeGeminiClient([_successful_response()])

    with logfire.span(_RUN_SPAN_NAME, run_id=str(_RUN_ID)):
        await _prepare(_service(client), question=question)

    run = one_span_named(capfire, _RUN_SPAN_NAME)
    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    provider = one_span_named(capfire, _PROVIDER_SPAN_NAME)
    assert provider["parent"]["span_id"] == phase["context"]["span_id"]
    assert phase["parent"]["span_id"] == run["context"]["span_id"]
    assert provider["context"]["trace_id"] == run["context"]["trace_id"]
    assert domain_attr_keys(phase["attributes"]) == {"phase", "agent_name"}
    assert phase["attributes"]["phase"] == "question_context"
    assert phase["attributes"]["agent_name"] == QUESTION_CONTEXT_AGENT.name
    assert provider["attributes"]["attempt_number"] == 1
    assert provider["attributes"]["result"] == "succeeded"
    assert provider["attributes"]["gen_ai.operation.name"] == "generate_content"
    assert provider["attributes"]["gen_ai.provider.name"] == "gcp.gemini"
    assert provider["attributes"]["gen_ai.request.model"] == (
        QUESTION_CONTEXT_AGENT.model.name
    )
    assert provider["attributes"]["gen_ai.usage.input_tokens"] == 13
    assert provider["attributes"]["gen_ai.usage.output_tokens"] == 5
    assert not any(key.startswith("gen_ai.usage.") for key in phase["attributes"])


async def test_request_separates_fixed_instructions_from_model_visible_input() -> None:
    question = "QUESTION_SENTINEL_480a"
    client = FakeGeminiClient([_successful_response()])

    await _prepare(_service(client), question=question)

    request = client.models.generate_content.await_args.kwargs
    contents = request["contents"]
    instructions = request["config"].system_instruction
    assert instructions == QUESTION_CONTEXT_AGENT.prompt.instructions
    assert question in contents
    assert "HISTORY_SENTINEL_7ac1" in contents
    assert "MISSING_SENTINEL_c31f" in contents
    assert question not in instructions
    assert "HISTORY_SENTINEL_7ac1" not in instructions
    assert "MISSING_SENTINEL_c31f" not in instructions
    assert "履歴にない事実、要望、目的を補完・推測しない" in instructions
    assert "履歴にない事実、要望、目的を補完・推測しない" not in contents


async def test_invalid_response_fallback_keeps_phase_non_error(
    capfire: CaptureLogfire,
) -> None:
    invalid_sentinel = "INVALID_RESPONSE_SENTINEL_f77a"
    client = FakeGeminiClient([FakeResponse(text=invalid_sentinel)])

    result = await _prepare(_service(client), question="fallback question")

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    provider = one_span_named(capfire, _PROVIDER_SPAN_NAME)
    assert result.context.standalone_question == "fallback question"
    assert provider["attributes"]["result"] == "invalid_response"
    assert provider["parent"]["span_id"] == phase["context"]["span_id"]
    assert exception_event(phase) is None
    assert phase.get("status", {}).get("description") in (None, "")
    assert invalid_sentinel not in json.dumps(
        [phase, provider],
        default=str,
        ensure_ascii=False,
    )


async def test_unknown_error_is_redacted_in_phase_and_attempt(
    capfire: CaptureLogfire,
) -> None:
    install_exception_redaction()
    error_sentinel = "UNCLASSIFIED_CONTEXT_SENTINEL_e081"
    error = RuntimeError(error_sentinel)
    client = FakeGeminiClient([error])

    with pytest.raises(RuntimeError) as raised:
        await _prepare(_service(client), question="QUESTION_SENTINEL_04c7")

    phase = one_span_named(capfire, _PHASE_SPAN_NAME)
    provider = one_span_named(capfire, _PROVIDER_SPAN_NAME)
    raw_phases = [
        span
        for span in capfire.exporter.exported_spans
        if span.name == _PHASE_SPAN_NAME
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    assert raised.value is error
    assert exception_event(phase)["attributes"]["exception.message"] == "[redacted]"
    assert exception_event(provider)["attributes"]["exception.message"] == (
        "[redacted]"
    )
    assert raw_phases[0].status.status_code is StatusCode.ERROR
    assert error_sentinel not in json.dumps(
        [phase, provider],
        default=str,
        ensure_ascii=False,
    )


async def test_unavailable_runtime_creates_no_phase_or_attempt(
    capfire: CaptureLogfire,
) -> None:
    service = QuestionContextService(
        agent=QUESTION_CONTEXT_AGENT,
        runtime_scope_factory=None,
    )

    await _prepare(service, question="NVIDIA の直近発表は？")

    assert spans_named(capfire, _PHASE_SPAN_NAME) == []
    assert spans_named(capfire, _PROVIDER_SPAN_NAME) == []
