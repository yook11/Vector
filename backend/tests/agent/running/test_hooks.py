"""Question resolved run hook の public behavior tests。"""

from __future__ import annotations

import pytest

import app.agent.running as running_module
from app.agent.contract import AnswerProgressEvent, QuestionResolvedEvent
from app.agent.question_context import AnswerBrief
from app.agent.running.hooks import QuestionResolvedRunHooks

HOOK_TYPE = "QuestionResolvedRunHooks"


class _FakeAnswerEventReporter:
    def __init__(self) -> None:
        self.events: list[AnswerProgressEvent] = []

    async def event_occurred(self, event: AnswerProgressEvent) -> None:
        self.events.append(event)


def _hook(reporter: _FakeAnswerEventReporter) -> QuestionResolvedRunHooks:
    return QuestionResolvedRunHooks(events=reporter)


def test_question_resolved_run_hooks_is_a_public_running_export() -> None:
    assert (
        getattr(running_module, HOOK_TYPE, None) is QuestionResolvedRunHooks
        and HOOK_TYPE in running_module.__all__
    )


async def test_history_rewrite_notifies_completed_standalone_question_once() -> None:
    reporter = _FakeAnswerEventReporter()
    hook = _hook(reporter)
    answer_brief = AnswerBrief(
        standalone_question="NVIDIA の発表が投資へ与える影響は？",
        answer_requirements=("株価への影響を含める",),
        relevant_prior_coverage="発表内容は説明済み",
        active_goal="半導体投資を調査する",
    )
    original_context = answer_brief.model_dump()

    returned = await hook.on_answer_brief_prepared(
        original_question="それが投資へ与える影響は？",
        has_history=True,
        answer_brief=answer_brief,
    )

    assert (
        returned,
        [event.model_dump() for event in reporter.events],
        all(isinstance(event, QuestionResolvedEvent) for event in reporter.events),
        answer_brief.model_dump(),
    ) == (
        None,
        [
            {
                "type": "context_resolution.question_resolved",
                "standalone_question": answer_brief.standalone_question,
            }
        ],
        True,
        original_context,
    )


@pytest.mark.parametrize(
    ("original_question", "has_history", "answer_brief"),
    [
        pytest.param(
            "それが投資へ与える影響は？",
            False,
            AnswerBrief(standalone_question="NVIDIA の発表が投資へ与える影響は？"),
            id="initial-question-even-if-rewritten",
        ),
        pytest.param(
            "  NVIDIA の直近発表は？\n",
            True,
            AnswerBrief(standalone_question="NVIDIA の直近発表は？"),
            id="history-echo-after-strip",
        ),
        pytest.param(
            "NVIDIA の直近発表は？",
            True,
            AnswerBrief(
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
    answer_brief: AnswerBrief,
) -> None:
    reporter = _FakeAnswerEventReporter()

    returned = await _hook(reporter).on_answer_brief_prepared(
        original_question=original_question,
        has_history=has_history,
        answer_brief=answer_brief,
    )

    assert (returned, reporter.events) == (None, [])
