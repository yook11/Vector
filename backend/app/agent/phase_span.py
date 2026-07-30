"""Researcher と AnsweringRunner が共有する task 単位 Agent policy span。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import logfire
from opentelemetry.trace import StatusCode

__all__ = ["agent_phase"]

_PHASE_SPAN_NAME = "agent_phase"


@contextmanager
def agent_phase(
    *, phase: str, agent_name: str, task_index: int | None = None
) -> Iterator[None]:
    """Agent policy spanを作る。task_indexを渡すとtask単位のspanになる。"""
    if task_index is not None and task_index < 0:
        raise ValueError("task_index must be non-negative")
    attributes = {"phase": phase, "agent_name": agent_name}
    if task_index is not None:
        attributes["task_index"] = task_index
    with logfire.span(_PHASE_SPAN_NAME, **attributes) as span:
        try:
            yield
        except BaseException:
            span.set_status(StatusCode.ERROR, "unclassified agent phase error")
            raise
