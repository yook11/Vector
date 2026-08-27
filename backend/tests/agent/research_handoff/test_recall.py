"""recall_research_handoff() の検証契約。"""

from __future__ import annotations

from datetime import UTC, datetime

from logfire.testing import CaptureLogfire

from app.agent.research_handoff import (
    ResearchHandoff,
    ResearchRunRecord,
    ResearchTaskRecord,
    recall_research_handoff,
)
from tests.logfire._span_helpers import spans_named

_AS_OF = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def _valid_raw(*, research_goal: str) -> dict[str, object]:
    return ResearchHandoff(
        updated_at=_AS_OF,
        runs=(
            ResearchRunRecord(
                as_of=_AS_OF,
                tasks=(
                    ResearchTaskRecord(
                        research_goal=research_goal,
                        executed_queries=("q",),
                        adopted_claims=(),
                    ),
                ),
                unresolved_after_search=(),
            ),
        ),
    ).model_dump(mode="json")


def test_recall_restores_the_stored_handoff() -> None:
    recalled = recall_research_handoff(_valid_raw(research_goal="goal"))

    assert recalled is not None
    assert recalled.runs[0].tasks[0].research_goal == "goal"


def test_recall_returns_none_for_a_thread_without_a_handoff() -> None:
    assert recall_research_handoff(None) is None


def test_recall_returns_none_without_raising_when_the_stored_handoff_is_invalid(
    capfire: CaptureLogfire,
) -> None:
    """書込み後のschema変更で無効になったhandoffは、無かったものとして扱う。"""
    recalled = recall_research_handoff({"schema_version": 1, "runs": []})

    assert recalled is None
    invalid_logs = spans_named(capfire, "research_handoff_recall_invalid_skipped")
    assert len(invalid_logs) == 1
    assert invalid_logs[0]["attributes"]["failure_code"] == "invalid_handoff_skipped"
