"""Evidence Answer工程とprovider attemptのtrace境界。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from google.genai.client import AsyncClient
from logfire.testing import CaptureLogfire
from opentelemetry.trace import StatusCode

from app.agent.answering.contract import AnsweringRequest
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.answering.evidence_answer.evidence import AnswerInputEvidence
from app.agent.answering.evidence_answer.service import EvidenceAnswerService
from app.agent.contract import ExternalUrlSource
from app.agent.planning.contract import TargetTimeWindow
from app.agent.runtime.contract import StreamingAgentRuntime
from app.agent.runtime.gemini import GeminiAgentRuntime
from tests.agent.runtime._helpers import FakeGeminiClient
from tests.agent.runtime._tracing_helpers import span_text


class _SdkStream:
    def __init__(self, text: str) -> None:
        self._chunks = iter(
            [
                SimpleNamespace(
                    text=text,
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


def _request() -> AnsweringRequest:
    return AnsweringRequest(
        question="MODEL_QUESTION_SENTINEL",
        as_of=datetime(2026, 8, 28, tzinfo=UTC),
    )


def _evidence() -> AnswerInputEvidence:
    return AnswerInputEvidence(
        source=ExternalUrlSource(
            source_ref="1",
            url="https://example.com/evidence",
            title="MODEL_EVIDENCE_TITLE_SENTINEL",
            evidence_claim="MODEL_EVIDENCE_CLAIM_SENTINEL",
        ),
        text="MODEL_EVIDENCE_TEXT_SENTINEL",
    )


async def test_phase_owns_all_provider_attempts_without_model_text(
    capfire: CaptureLogfire,
) -> None:
    first_stream = _SdkStream("引用がない初回回答")
    second_stream = _SdkStream("MODEL_ANSWER_SENTINEL [[1]]")
    client = FakeGeminiClient([], streams=[first_stream, second_stream])

    @asynccontextmanager
    async def runtime_scope() -> AsyncIterator[StreamingAgentRuntime]:
        yield GeminiAgentRuntime(client=cast(AsyncClient, client))

    draft = await EvidenceAnswerService(
        agent=EVIDENCE_ANSWER_AGENT,
        runtime_scope_factory=runtime_scope,
    ).answer(
        request=_request(),
        evidence=[_evidence()],
        target_time_window=TargetTimeWindow(kind="today"),
        review_missing=(),
    )

    spans = capfire.exporter.exported_spans
    phase_spans = [
        span
        for span in spans
        if span.name == "agent_phase"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]
    attempt_spans = [
        span
        for span in spans
        if span.name == "agent_provider_call"
        and (span.attributes or {}).get("logfire.span_type") == "span"
    ]

    assert draft.answer == "MODEL_ANSWER_SENTINEL [[1]]"
    assert len(phase_spans) == 1
    assert len(attempt_spans) == 2
    phase = phase_spans[0]
    assert (phase.attributes or {})["phase"] == "answering"
    assert (phase.attributes or {})["agent_name"] == "evidence_answer"
    assert phase.status.status_code is StatusCode.UNSET
    for attempt in attempt_spans:
        assert attempt.parent is not None
        assert attempt.parent.span_id == phase.context.span_id
        assert attempt.status.status_code is StatusCode.UNSET
        assert (attempt.attributes or {})["result"] == "succeeded"
    assert [stream.close_calls for stream in (first_stream, second_stream)] == [1, 1]

    observed = "\n".join(span_text(span) for span in [phase, *attempt_spans])
    assert "MODEL_QUESTION_SENTINEL" not in observed
    assert "MODEL_EVIDENCE_TITLE_SENTINEL" not in observed
    assert "MODEL_EVIDENCE_CLAIM_SENTINEL" not in observed
    assert "MODEL_EVIDENCE_TEXT_SENTINEL" not in observed
    assert "MODEL_ANSWER_SENTINEL" not in observed
