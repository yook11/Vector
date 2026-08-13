"""Question resolved run hook の public behavior tests。"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import pytest

from app.agent.contract import AnswerProgressEvent, QuestionResolvedEvent
from app.agent.question_context import QuestionContext

HOOKS_MODULE = "app.agent.running.hooks"
HOOK_TYPE = "QuestionResolvedRunHooks"


class _FakeAnswerEventReporter:
    def __init__(self) -> None:
        self.events: list[AnswerProgressEvent] = []

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        self.events.append(event)


def _hooks_module() -> ModuleType:
    missing_hooks = False
    try:
        return importlib.import_module(HOOKS_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == HOOKS_MODULE or exc.name.startswith(f"{HOOKS_MODULE}."):
            missing_hooks = True
        else:
            raise
    if missing_hooks:
        pytest.fail(
            "app.agent.running.hooks.QuestionResolvedRunHooks が未実装です",
            pytrace=False,
        )
    raise AssertionError("unreachable")


def _hook_type() -> type[Any]:
    hook_type = getattr(_hooks_module(), HOOK_TYPE, None)
    if hook_type is None:
        pytest.fail(
            "app.agent.running.hooks must define QuestionResolvedRunHooks",
            pytrace=False,
        )
    return hook_type


def _hook(reporter: _FakeAnswerEventReporter) -> Any:
    return _hook_type()(events=reporter)


def test_question_resolved_run_hooks_is_a_public_running_export() -> None:
    hook_type = _hook_type()
    running = importlib.import_module("app.agent.running")

    assert (
        getattr(running, HOOK_TYPE, None) is hook_type and HOOK_TYPE in running.__all__
    )


async def test_history_rewrite_notifies_completed_standalone_question_once() -> None:
    reporter = _FakeAnswerEventReporter()
    hook = _hook(reporter)
    question_context = QuestionContext(
        standalone_question="NVIDIA の発表が投資へ与える影響は？",
        answer_requirements=("株価への影響を含める",),
        relevant_prior_coverage="発表内容は説明済み",
        active_goal="半導体投資を調査する",
    )
    original_context = question_context.model_dump()

    returned = await hook.on_answering_context_prepared(
        original_question="それが投資へ与える影響は？",
        has_history=True,
        question_context=question_context,
    )

    assert (
        returned,
        [event.model_dump() for event in reporter.events],
        all(isinstance(event, QuestionResolvedEvent) for event in reporter.events),
        question_context.model_dump(),
    ) == (
        None,
        [
            {
                "type": "context_resolution.question_resolved",
                "standalone_question": question_context.standalone_question,
            }
        ],
        True,
        original_context,
    )


@pytest.mark.parametrize(
    ("original_question", "has_history", "question_context"),
    [
        pytest.param(
            "それが投資へ与える影響は？",
            False,
            QuestionContext(standalone_question="NVIDIA の発表が投資へ与える影響は？"),
            id="initial-question-even-if-rewritten",
        ),
        pytest.param(
            "  NVIDIA の直近発表は？\n",
            True,
            QuestionContext(standalone_question="NVIDIA の直近発表は？"),
            id="history-echo-after-strip",
        ),
        pytest.param(
            "NVIDIA の直近発表は？",
            True,
            QuestionContext(
                standalone_question="NVIDIA の直近発表は？",
                answer_requirements=("NVIDIA の直近発表は？",),
            ),
            id="history-safe-fallback",
        ),
    ],
)
async def test_non_rewrite_conditions_do_not_notify(
    original_question: str,
    has_history: bool,
    question_context: QuestionContext,
) -> None:
    reporter = _FakeAnswerEventReporter()

    returned = await _hook(reporter).on_answering_context_prepared(
        original_question=original_question,
        has_history=has_history,
        question_context=question_context,
    )

    assert (returned, reporter.events) == (None, [])
