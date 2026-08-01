"""Agent の工程 span。所属工程と終了分類を1箇所で決める。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import logfire
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import StatusCode

from app.agent.contract import AnswerGenerationStopped
from app.agent.error_type import span_error_type
from app.agent.runtime.contract import AgentResponseInvalidError
from app.analysis.ai_provider_errors import AIProviderError

__all__ = ["AgentPhase", "agent_phase"]

AgentPhase = Literal[
    "safety_check",
    "context_resolution",
    "planning",
    "evidence_collection",
    "evidence_review",
    "answering",
]

_PHASE_SPAN_NAME = "agent_phase"

# 工程が自分で扱える終了。span を error にせず、同じ例外を span 終了後に再送出する。
_PHASE_STOPS = (AnswerGenerationStopped,)
_CLASSIFIED_FAILURES = (AIProviderError, AgentResponseInvalidError)


@contextmanager
def agent_phase(
    *,
    phase: AgentPhase,
    agent_name: str | None = None,
    task_index: int | None = None,
) -> Iterator[None]:
    """工程spanを作る。agent_nameはAgentが実行する工程だけ、task_indexはtask単位のとき渡す。"""
    if task_index is not None and task_index < 0:
        raise ValueError("task_index must be non-negative")
    attributes: dict[str, str | int] = {"phase": phase}
    if agent_name is not None:
        attributes["agent_name"] = agent_name
    if task_index is not None:
        attributes["task_index"] = task_index

    deferred: BaseException | None = None
    with logfire.span(_PHASE_SPAN_NAME, **attributes) as span:
        # 未分類例外は貫通させ、logfire の自動例外記録に status と event を任せる。
        try:
            yield
        except _PHASE_STOPS as stop:
            deferred = stop
        except _CLASSIFIED_FAILURES as failure:
            span.set_attribute(ERROR_TYPE, span_error_type(failure))
            span.set_status(StatusCode.ERROR)
            deferred = failure
    if deferred is not None:
        raise deferred
