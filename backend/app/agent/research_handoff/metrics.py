"""申し送り整理工程のメトリクス。"""

from __future__ import annotations

from typing import Literal

import logfire

__all__ = ["ResearchHandoffOutcomeResult", "record_research_handoff_outcome"]

ResearchHandoffOutcomeResult = Literal["organized", "failed"]

_outcome_counter = logfire.metric_counter(
    "vector.agent.research_handoff.outcome",
    unit="1",
    description="Research handoff organize outcome per agent run",
)


def record_research_handoff_outcome(
    *,
    result: ResearchHandoffOutcomeResult,
    failure_code: str | None = None,
) -> None:
    """整理の結末を、会話内容を含めずに記録する。"""
    _outcome_counter.add(
        1,
        attributes={
            "result": result,
            "failure_code": failure_code if failure_code is not None else "none",
        },
    )
