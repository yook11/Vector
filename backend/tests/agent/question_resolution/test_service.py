"""Question resolution service tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from structlog.testing import capture_logs

from app.agent.conversations.contracts import ThreadMessageSnapshot
from app.agent.question_resolution.contract import ResolvedQuestionDraft
from app.agent.question_resolution.service import (
    HISTORY_MESSAGE_CHAR_CAP,
    QuestionResolutionResponseInvalidError,
    QuestionResolutionService,
)
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_OUTCOME_METRIC = "vector.agent.question_resolution.outcome"
_RUN_ID = UUID("00000000-0000-4000-a000-000000000020")


class FakeResolver:
    def __init__(self, outcome: ResolvedQuestionDraft | Exception) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def resolve(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> ResolvedQuestionDraft:
        self.calls.append({"question": question, "history": history, "as_of": as_of})
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _as_of() -> datetime:
    return datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


def _history() -> list[ThreadMessageSnapshot]:
    return [ThreadMessageSnapshot(role="assistant", content="直前の回答")]


@pytest.mark.asyncio
async def test_empty_history_skips_resolver_and_returns_passthrough(
    capfire: CaptureLogfire,
) -> None:
    resolver = FakeResolver(AssertionError("resolver must not be called"))

    resolved = await QuestionResolutionService(resolver=resolver).resolve(
        question="NVIDIA の直近発表は？",
        history=[],
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert resolved.standalone_question == "NVIDIA の直近発表は？"
    assert resolved.user_intent == ""
    assert resolved.prior_coverage == ""
    assert resolved.user_activity_context == ""
    assert resolver.calls == []
    assert (
        sum_counter_for_result(collected_metrics(capfire), _OUTCOME_METRIC, "skipped")
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [AIProviderNetworkError(), QuestionResolutionResponseInvalidError("not_json")],
)
async def test_typed_resolution_failure_returns_passthrough_without_leaking_question(
    failure: Exception,
    capfire: CaptureLogfire,
) -> None:
    secret_question = "SECRET_USER_QUESTION"
    resolver = FakeResolver(failure)

    with capture_logs() as logs:
        resolved = await QuestionResolutionService(resolver=resolver).resolve(
            question=secret_question,
            history=_history(),
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert resolved.standalone_question == secret_question
    assert resolved.user_intent == ""
    assert len(resolver.calls) == 1
    assert logs[0]["event"] == "question_resolution_failed"
    assert logs[0]["run_id"] == str(_RUN_ID)
    assert "SECRET_USER_QUESTION" not in repr(logs)
    assert (
        sum_counter_for_result(collected_metrics(capfire), _OUTCOME_METRIC, "failed")
        == 1
    )


@pytest.mark.asyncio
async def test_success_forwards_cleaned_structured_context_and_caps_history(
    capfire: CaptureLogfire,
) -> None:
    resolver = FakeResolver(
        ResolvedQuestionDraft(
            standalone_question="  NVIDIA の株価への影響は？  ",
            user_intent="  詳しく説明して  ",
            prior_coverage="  すでに発表内容を説明済み  ",
            user_activity_context="  半導体投資を調査中  ",
        )
    )
    history = [
        ThreadMessageSnapshot(
            role="assistant", content="x" * (HISTORY_MESSAGE_CHAR_CAP + 1)
        )
    ]

    resolved = await QuestionResolutionService(resolver=resolver).resolve(
        question="それの影響は？",
        history=history,
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert resolved.standalone_question == "NVIDIA の株価への影響は？"
    assert resolved.user_intent == "詳しく説明して"
    assert resolved.prior_coverage == "すでに発表内容を説明済み"
    assert resolved.user_activity_context == "半導体投資を調査中"
    assert len(resolver.calls[0]["history"][0].content) == HISTORY_MESSAGE_CHAR_CAP
    assert (
        sum_counter_for_result(collected_metrics(capfire), _OUTCOME_METRIC, "resolved")
        == 1
    )
