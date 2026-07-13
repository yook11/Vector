"""Question context preparation service tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError
from structlog.testing import capture_logs

from app.agent.question_context.contract import QuestionContextDraft
from app.agent.question_context.service import (
    HISTORY_MESSAGE_CHAR_CAP,
    QuestionContextResponseInvalidError,
    QuestionContextService,
    _history_for_prompt,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderNetworkError
from tests.logfire._metric_helpers import collected_metrics

_OUTCOME_METRIC = "vector.agent.question_context.outcome"
_LEGACY_OUTCOME_METRIC = "vector.agent.question_resolution.outcome"
_RUN_ID = UUID("00000000-0000-4000-a000-000000000020")


class FakeGenerator:
    def __init__(self, outcome: QuestionContextDraft | Exception) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> QuestionContextDraft:
        self.calls.append({"question": question, "history": history, "as_of": as_of})
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _as_of() -> datetime:
    return datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


def _validation_error() -> ValidationError:
    try:
        QuestionContextDraft.model_validate({"standalone_question": None})
    except ValidationError as exc:
        return exc
    raise AssertionError("invalid draft must raise ValidationError")


def _question_context_outcomes(
    capfire: CaptureLogfire,
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (metric["name"], data_point.get("attributes", {}))
        for metric in collected_metrics(capfire)
        if metric["name"] in {_OUTCOME_METRIC, _LEGACY_OUTCOME_METRIC}
        for data_point in metric["data"]["data_points"]
    ]


def test_thread_message_snapshot_carries_missing_aspects_as_a_tuple() -> None:
    assistant_snapshot = ThreadMessageSnapshot(
        role="assistant",
        content="直前の回答",
        missing_aspects=("保存済みの不足",),
    )
    user_snapshot = ThreadMessageSnapshot(role="user", content="前の質問")

    assert (assistant_snapshot.missing_aspects, user_snapshot.missing_aspects) == (
        ("保存済みの不足",),
        (),
    )


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


@pytest.mark.asyncio
async def test_empty_history_calls_generator_and_fixes_initial_context_values(
    capfire: CaptureLogfire,
) -> None:
    question = "NVIDIA の直近発表は？"
    generator = FakeGenerator(
        QuestionContextDraft(
            standalone_question="書き換えた質問",
            content_requirements=["NVIDIA の発表内容"],
            response_requirements=["表形式で回答する"],
            relevant_prior_coverage="履歴がないため採用しない",
            active_goal="投資判断をする",
            explicit_feedback_detected=True,
        )
    )

    result = await QuestionContextService(generator=generator).prepare(
        question=question,
        history=[],
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert (
        generator.calls,
        result.context.model_dump(),
        result.telemetry.model_dump(),
        _question_context_outcomes(capfire),
    ) == (
        [{"question": question, "history": [], "as_of": _as_of()}],
        {
            "standalone_question": question,
            "content_requirements": [
                {"requirement_id": "c1", "description": "NVIDIA の発表内容"}
            ],
            "response_requirements": [
                {"requirement_id": "p1", "description": "表形式で回答する"}
            ],
            "relevant_prior_coverage": "",
            "active_goal": "投資判断をする",
        },
        {
            "explicit_feedback_detected": False,
            "previous_answer_had_missing_aspects": False,
        },
        [
            (
                _OUTCOME_METRIC,
                {
                    "result": "prepared",
                    "explicit_feedback_detected": False,
                    "previous_answer_had_missing_aspects": False,
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_history_success_returns_preparation_result_and_uses_latest_missing_flag(
    capfire: CaptureLogfire,
) -> None:
    history = [
        ThreadMessageSnapshot(role="user", content="前の質問"),
        ThreadMessageSnapshot(
            role="assistant",
            content="以前の回答",
            missing_aspects=("以前の不足",),
        ),
        ThreadMessageSnapshot(
            role="assistant",
            content="直前の回答",
            missing_aspects=(),
        ),
    ]
    generator = FakeGenerator(
        QuestionContextDraft(
            standalone_question="NVIDIA の株価への影響は？",
            content_requirements=["株価への影響"],
            response_requirements=["詳しく説明する"],
            relevant_prior_coverage="発表内容は説明済み",
            active_goal="半導体投資を調査する",
            explicit_feedback_detected=True,
        )
    )

    result = await QuestionContextService(generator=generator).prepare(
        question="それの影響は？",
        history=history,
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert (
        generator.calls,
        result.context.model_dump(),
        result.telemetry.model_dump(),
        _question_context_outcomes(capfire),
    ) == (
        [
            {
                "question": "それの影響は？",
                "history": history,
                "as_of": _as_of(),
            }
        ],
        {
            "standalone_question": "NVIDIA の株価への影響は？",
            "content_requirements": [
                {"requirement_id": "c1", "description": "株価への影響"}
            ],
            "response_requirements": [
                {"requirement_id": "p1", "description": "詳しく説明する"}
            ],
            "relevant_prior_coverage": "発表内容は説明済み",
            "active_goal": "半導体投資を調査する",
        },
        {
            "explicit_feedback_detected": True,
            "previous_answer_had_missing_aspects": False,
        },
        [
            (
                _OUTCOME_METRIC,
                {
                    "result": "prepared",
                    "explicit_feedback_detected": True,
                    "previous_answer_had_missing_aspects": False,
                },
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        AIProviderNetworkError(),
        QuestionContextResponseInvalidError("not_json"),
        _validation_error(),
    ],
)
async def test_known_generator_failures_return_safe_fallback_without_leaking_question(
    failure: Exception,
    capfire: CaptureLogfire,
) -> None:
    secret_question = "SECRET_USER_QUESTION"
    history = [
        ThreadMessageSnapshot(
            role="assistant",
            content="直前の回答",
            missing_aspects=("保存済みの不足",),
        )
    ]
    generator = FakeGenerator(failure)

    with capture_logs() as logs:
        result = await QuestionContextService(generator=generator).prepare(
            question=secret_question,
            history=history,
            as_of=_as_of(),
            run_id=_RUN_ID,
        )

    assert (
        result.context.model_dump(),
        result.telemetry.model_dump(),
        [(log["event"], log["run_id"]) for log in logs],
        secret_question not in repr(logs),
        _question_context_outcomes(capfire),
    ) == (
        {
            "standalone_question": secret_question,
            "content_requirements": [
                {"requirement_id": "c1", "description": secret_question}
            ],
            "response_requirements": [],
            "relevant_prior_coverage": "",
            "active_goal": "",
        },
        {
            "explicit_feedback_detected": False,
            "previous_answer_had_missing_aspects": True,
        },
        [("question_context_preparation_failed", str(_RUN_ID))],
        True,
        [
            (
                _OUTCOME_METRIC,
                {
                    "result": "failed",
                    "explicit_feedback_detected": False,
                    "previous_answer_had_missing_aspects": True,
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_missing_generator_returns_safe_fallback(
    capfire: CaptureLogfire,
) -> None:
    question = "NVIDIA の直近発表は？"

    result = await QuestionContextService(generator=None).prepare(
        question=question,
        history=[ThreadMessageSnapshot(role="assistant", content="直前の回答")],
        as_of=_as_of(),
        run_id=_RUN_ID,
    )

    assert (
        result.context.model_dump(),
        result.telemetry.model_dump(),
        _question_context_outcomes(capfire),
    ) == (
        {
            "standalone_question": question,
            "content_requirements": [{"requirement_id": "c1", "description": question}],
            "response_requirements": [],
            "relevant_prior_coverage": "",
            "active_goal": "",
        },
        {
            "explicit_feedback_detected": False,
            "previous_answer_had_missing_aspects": False,
        },
        [
            (
                _OUTCOME_METRIC,
                {
                    "result": "failed",
                    "explicit_feedback_detected": False,
                    "previous_answer_had_missing_aspects": False,
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_unexpected_generator_exception_propagates() -> None:
    generator = FakeGenerator(RuntimeError("unexpected generator failure"))

    with pytest.raises(RuntimeError, match="unexpected generator failure"):
        await QuestionContextService(generator=generator).prepare(
            question="NVIDIA の直近発表は？",
            history=[ThreadMessageSnapshot(role="assistant", content="直前の回答")],
            as_of=_as_of(),
            run_id=_RUN_ID,
        )
