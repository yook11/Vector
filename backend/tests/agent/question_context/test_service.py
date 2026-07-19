"""Question Context Service policy tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError
from structlog.testing import capture_logs

from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.question_context.contract import (
    QuestionContextDraft,
    QuestionContextGenerationInput,
)
from app.agent.question_context.service import (
    HISTORY_MESSAGE_CHAR_CAP,
    QuestionContextService,
    _history_for_prompt,
)
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import (
    AIProviderError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.gemini_error_translator import GeminiContentRejectionReason
from tests.agent.runtime._fakes import ScriptedAgentRuntime
from tests.logfire._metric_helpers import collected_metrics
from tests.logfire._span_helpers import spans_named

_OUTCOME_METRIC = "vector.agent.question_context.outcome"
_RUN_ID = UUID("00000000-0000-4000-a000-000000000020")


def _runtime_validation_error() -> ValidationError:
    try:
        QuestionContextDraft.model_validate({"standalone_question": {}})
    except ValidationError as error:
        return error
    raise AssertionError("invalid draft fixture must raise ValidationError")


class _RuntimeScope:
    def __init__(
        self,
        factory: RecordingRuntimeScopeFactory,
        runtime: ScriptedAgentRuntime,
    ) -> None:
        self._factory = factory
        self._runtime = runtime

    async def __aenter__(self) -> ScriptedAgentRuntime:
        self._factory.entered += 1
        if self._factory.enter_error is not None:
            raise self._factory.enter_error
        return self._runtime

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._factory.exits.append((exc_type, exc, traceback))
        return False


class RecordingRuntimeScopeFactory:
    def __init__(
        self,
        runtime: ScriptedAgentRuntime,
        *,
        enter_error: BaseException | None = None,
    ) -> None:
        self.runtime = runtime
        self.enter_error = enter_error
        self.created = 0
        self.entered = 0
        self.exits: list[
            tuple[
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ]
        ] = []

    def __call__(self) -> _RuntimeScope:
        self.created += 1
        return _RuntimeScope(self, self.runtime)


def _as_of() -> datetime:
    return datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


def _service(
    outcomes: list[QuestionContextDraft | BaseException],
) -> tuple[
    QuestionContextService,
    ScriptedAgentRuntime,
    RecordingRuntimeScopeFactory,
]:
    runtime = ScriptedAgentRuntime(outcomes)
    factory = RecordingRuntimeScopeFactory(runtime)
    return (
        QuestionContextService(
            agent=QUESTION_CONTEXT_AGENT,
            runtime_scope_factory=factory,
        ),
        runtime,
        factory,
    )


def _question_context_outcomes(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        data_point.get("attributes", {})
        for metric in collected_metrics(capfire)
        if metric["name"] == _OUTCOME_METRIC
        for data_point in metric["data"]["data_points"]
    ]


def _base_metric(
    *,
    result: str,
    explicit_feedback_detected: bool,
    previous_answer_had_missing_aspects: bool,
    failure_code: str | None = None,
) -> dict[str, Any]:
    metric = {
        "result": result,
        "explicit_feedback_detected": explicit_feedback_detected,
        "previous_answer_had_missing_aspects": (previous_answer_had_missing_aspects),
        "prompt_version": QUESTION_CONTEXT_AGENT.prompt.version,
        "ai_model": QUESTION_CONTEXT_AGENT.model.name,
    }
    if failure_code is not None:
        metric["failure_code"] = failure_code
    return metric


def test_history_for_prompt_caps_content_and_normalizes_saved_missing_aspects() -> None:
    history = [
        ThreadMessageSnapshot(
            role="assistant",
            content="x" * (HISTORY_MESSAGE_CHAR_CAP + 1),
            missing_aspects=(
                " first ",
                "first",
                "x" * 301,
                "second",
                "third",
                "fourth",
            ),
        ),
        ThreadMessageSnapshot(role="user", content="follow-up"),
        ThreadMessageSnapshot(
            role="assistant",
            content="latest answer",
            missing_aspects=(
                "second",
                "fifth",
                "sixth",
                "seventh",
                "eighth",
                "ninth",
            ),
        ),
    ]

    assert _history_for_prompt(history) == [
        ThreadMessageSnapshot(
            role="assistant",
            content="x" * HISTORY_MESSAGE_CHAR_CAP,
            missing_aspects=("first", "x" * 300, "second", "third", "fourth"),
        ),
        ThreadMessageSnapshot(role="user", content="follow-up"),
        ThreadMessageSnapshot(
            role="assistant",
            content="latest answer",
            missing_aspects=("fifth", "sixth", "seventh"),
        ),
    ]


async def test_prepare_activates_one_scope_and_invokes_agent_once() -> None:
    history = [ThreadMessageSnapshot(role="user", content="前の質問")]
    service, runtime, factory = _service(
        [QuestionContextDraft(standalone_question="整理済みの質問")]
    )

    await service.prepare(
        question="それについて教えて",
        history=history,
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert (factory.created, factory.entered, len(factory.exits)) == (1, 1, 1)
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call.agent is QUESTION_CONTEXT_AGENT
    assert call.attempt_number == 1
    assert call.input == QuestionContextGenerationInput(
        question="それについて教えて",
        history=tuple(history),
        as_of=_as_of(),
    )


async def test_empty_history_preserves_deterministic_context_correction(
    capfire: CaptureLogfire,
) -> None:
    question = "NVIDIA の直近発表は？"
    service, _runtime, _factory = _service(
        [
            QuestionContextDraft(
                standalone_question="書き換えた質問",
                content_requirements=["NVIDIA の発表内容"],
                response_requirements=["表形式で回答する"],
                relevant_prior_coverage="履歴がないため採用しない",
                active_goal="投資判断をする",
                explicit_feedback_detected=True,
            )
        ]
    )

    result = await service.prepare(
        question=question,
        history=[],
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert result.context.model_dump() == {
        "standalone_question": question,
        "content_requirements": [
            {"requirement_id": "c1", "description": "NVIDIA の発表内容"}
        ],
        "response_requirements": [
            {"requirement_id": "p1", "description": "表形式で回答する"}
        ],
        "relevant_prior_coverage": "",
        "active_goal": "投資判断をする",
    }
    assert result.telemetry.model_dump() == {
        "explicit_feedback_detected": False,
        "previous_answer_had_missing_aspects": False,
    }
    assert _question_context_outcomes(capfire) == [
        _base_metric(
            result="prepared",
            explicit_feedback_detected=False,
            previous_answer_had_missing_aspects=False,
        )
    ]


async def test_history_success_preserves_context_and_telemetry(
    capfire: CaptureLogfire,
) -> None:
    history = [
        ThreadMessageSnapshot(
            role="assistant",
            content="直前の回答",
            missing_aspects=("保存済みの不足",),
        )
    ]
    service, _runtime, _factory = _service(
        [
            QuestionContextDraft(
                standalone_question="NVIDIA の株価への影響は？",
                content_requirements=["株価への影響"],
                relevant_prior_coverage="発表内容は説明済み",
                active_goal="半導体投資を調査する",
                explicit_feedback_detected=True,
            )
        ]
    )

    result = await service.prepare(
        question="それの影響は？",
        history=history,
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert result.context.standalone_question == "NVIDIA の株価への影響は？"
    assert result.context.relevant_prior_coverage == "発表内容は説明済み"
    assert result.telemetry.explicit_feedback_detected is True
    assert result.telemetry.previous_answer_had_missing_aspects is True
    assert _question_context_outcomes(capfire) == [
        _base_metric(
            result="prepared",
            explicit_feedback_detected=True,
            previous_answer_had_missing_aspects=True,
        )
    ]


@pytest.mark.parametrize(
    ("error", "failure_code"),
    [
        pytest.param(
            AIProviderNetworkError(),
            "ai_error_network",
            id="provider",
        ),
        pytest.param(
            AIProviderOutputBlockedError(reason=GeminiContentRejectionReason.SAFETY),
            "ai_error_output_blocked",
            id="blocked",
        ),
        *[
            pytest.param(
                AgentResponseInvalidError(defect),
                defect.value,
                id=f"invalid-{defect.value}",
            )
            for defect in AgentResponseDefect
        ],
        pytest.param(AIProviderError(), "provider_error", id="provider-without-code"),
    ],
)
async def test_classified_failure_falls_back_once_with_stable_failure_code(
    error: BaseException,
    failure_code: str,
    capfire: CaptureLogfire,
) -> None:
    question = "MODEL_VISIBLE_QUESTION_SENTINEL_6ed1"
    service, runtime, factory = _service([error])

    with capture_logs() as logs:
        result = await service.prepare(
            question=question,
            history=[],
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert result.context.standalone_question == question
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    assert logs[0]["failure_type"] == failure_code
    assert type(error).__name__ not in logs[0]["failure_type"]
    assert question not in json.dumps(logs, default=str, ensure_ascii=False)
    assert _question_context_outcomes(capfire) == [
        _base_metric(
            result="failed",
            explicit_feedback_detected=False,
            previous_answer_had_missing_aspects=False,
            failure_code=failure_code,
        )
    ]


async def test_finalize_validation_failure_falls_back_without_leaking_draft(
    capfire: CaptureLogfire,
) -> None:
    draft_sentinel = "MODEL_DRAFT_SENTINEL_a83c"
    service, runtime, factory = _service(
        [
            QuestionContextDraft(
                standalone_question="   ",
                active_goal=draft_sentinel,
            )
        ]
    )

    with capture_logs() as logs:
        result = await service.prepare(
            question="安全なfallback質問",
            history=[ThreadMessageSnapshot(role="user", content="履歴")],
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert result.context.standalone_question == "安全なfallback質問"
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert factory.exits[0][0] is None
    assert logs[0]["failure_type"] == "context_finalize_invalid"
    observed = json.dumps(
        [logs, _question_context_outcomes(capfire)],
        default=str,
        ensure_ascii=False,
    )
    assert draft_sentinel not in observed


async def test_unavailable_runtime_skips_scope_and_records_stable_failure(
    capfire: CaptureLogfire,
) -> None:
    service = QuestionContextService(
        agent=QUESTION_CONTEXT_AGENT,
        runtime_scope_factory=None,
    )

    with capture_logs() as logs:
        result = await service.prepare(
            question="NVIDIA の直近発表は？",
            history=[],
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert result.context.standalone_question == "NVIDIA の直近発表は？"
    assert logs[0]["failure_type"] == "generator_unavailable"
    assert _question_context_outcomes(capfire) == [
        _base_metric(
            result="failed",
            explicit_feedback_detected=False,
            previous_answer_had_missing_aspects=False,
            failure_code="generator_unavailable",
        )
    ]


async def test_classified_scope_enter_failure_falls_back_without_attempt(
    capfire: CaptureLogfire,
) -> None:
    runtime = ScriptedAgentRuntime(
        [QuestionContextDraft(standalone_question="must not be consumed")]
    )
    factory = RecordingRuntimeScopeFactory(
        runtime,
        enter_error=AIProviderNetworkError(),
    )
    service = QuestionContextService(
        agent=QUESTION_CONTEXT_AGENT,
        runtime_scope_factory=factory,
    )

    with capture_logs() as logs:
        result = await service.prepare(
            question="fallback question",
            history=[],
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert result.context.standalone_question == "fallback question"
    assert runtime.calls == []
    assert (factory.created, factory.entered, factory.exits) == (1, 1, [])
    assert logs[0]["failure_type"] == "ai_error_network"
    assert spans_named(capfire, "agent_provider_call") == []
    assert _question_context_outcomes(capfire) == [
        _base_metric(
            result="failed",
            explicit_feedback_detected=False,
            previous_answer_had_missing_aspects=False,
            failure_code="ai_error_network",
        )
    ]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("unexpected context failure"), id="unknown"),
        pytest.param(
            _runtime_validation_error(),
            id="runtime-contract-validation-error",
        ),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_unknown_error_and_cancellation_propagate_by_identity(
    error: BaseException,
    capfire: CaptureLogfire,
) -> None:
    service, runtime, factory = _service([error])

    with pytest.raises(type(error)) as raised:
        await service.prepare(
            question="NVIDIA の直近発表は？",
            history=[],
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert raised.value is error
    assert [call.attempt_number for call in runtime.calls] == [1]
    assert len(factory.exits) == 1
    assert factory.exits[0][1] is error
    assert _question_context_outcomes(capfire) == []
