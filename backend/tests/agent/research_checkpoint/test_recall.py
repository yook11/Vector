"""recall_research_checkpoints() の検証・除外契約(注入フロー3)。"""

from __future__ import annotations

from datetime import UTC, datetime

from logfire.testing import CaptureLogfire

from app.agent.research_checkpoint.contract import (
    ResearchCheckpoint,
    ResearchTaskRecord,
)
from app.agent.research_checkpoint.recall import recall_research_checkpoints
from tests.logfire._span_helpers import spans_named

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def _valid_raw(*, research_goal: str, as_of: datetime = _AS_OF) -> dict[str, object]:
    return ResearchCheckpoint(
        as_of=as_of,
        tasks=(
            ResearchTaskRecord(
                research_goal=research_goal,
                executed_queries=("q",),
                adopted_claims=(),
            ),
        ),
        unresolved_after_search=(),
    ).model_dump(mode="json")


def test_recall_validates_each_raw_checkpoint_and_keeps_passed_order() -> None:
    raw_checkpoints = [
        _valid_raw(research_goal="goal-newest"),
        _valid_raw(research_goal="goal-oldest"),
    ]

    recalled = recall_research_checkpoints(raw_checkpoints)

    assert [checkpoint.tasks[0].research_goal for checkpoint in recalled] == [
        "goal-newest",
        "goal-oldest",
    ]
    assert all(isinstance(checkpoint, ResearchCheckpoint) for checkpoint in recalled)


def test_recall_excludes_only_the_invalid_element_and_keeps_order(
    capfire: CaptureLogfire,
) -> None:
    invalid_raw: dict[str, object] = {"schema_version": 1, "tasks": []}
    raw_checkpoints = [
        _valid_raw(research_goal="goal-first"),
        invalid_raw,
        _valid_raw(research_goal="goal-last"),
    ]

    recalled = recall_research_checkpoints(raw_checkpoints)

    assert [checkpoint.tasks[0].research_goal for checkpoint in recalled] == [
        "goal-first",
        "goal-last",
    ]
    invalid_logs = spans_named(capfire, "research_checkpoint_recall_invalid_skipped")
    assert len(invalid_logs) == 1
    assert invalid_logs[0]["attributes"]["invalid_count"] == 1
    assert invalid_logs[0]["attributes"]["failure_code"] == "invalid_checkpoint_skipped"


def test_recall_returns_empty_tuple_without_raising_when_all_elements_are_invalid(
    capfire: CaptureLogfire,
) -> None:
    raw_checkpoints = [{"not": "a checkpoint"}, {"still": "not a checkpoint"}]

    recalled = recall_research_checkpoints(raw_checkpoints)

    assert recalled == ()
    invalid_logs = spans_named(capfire, "research_checkpoint_recall_invalid_skipped")
    assert invalid_logs[0]["attributes"]["invalid_count"] == len(raw_checkpoints)
