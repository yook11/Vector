"""Low-cardinality metrics for agent run SSE connections."""

from __future__ import annotations

from typing import Literal

import logfire

AgentRunSseCloseReason = Literal[
    "terminal",
    "max_age",
    "unavailable",
    "client_disconnect",
    "cursor_trimmed",
    "queued_terminal",
    "queued_timeout",
    "lease_expired",
]
AgentRunSseCapacityScope = Literal["run", "user", "process"]

_active_connections = logfire.metric_up_down_counter(
    "vector.agent.sse.active_connections",
    unit="1",
    description="Active agent run SSE connections in this API process",
)
_connection_duration = logfire.metric_histogram(
    "vector.agent.sse.connection_duration",
    unit="s",
    description="Agent run SSE connection duration",
)
_connection_close = logfire.metric_counter(
    "vector.agent.sse.connection_close",
    unit="1",
    description="Agent run SSE connection close reason",
)
_capacity_rejection = logfire.metric_counter(
    "vector.agent.sse.capacity_rejection",
    unit="1",
    description="Agent run SSE capacity rejection scope",
)
_projection_drop = logfire.metric_counter(
    "vector.agent.sse.projection_drop",
    unit="1",
    description="Agent run event dropped by the public SSE allowlist",
)


def record_agent_run_sse_open() -> None:
    _active_connections.add(1)


def record_agent_run_sse_close(
    *,
    duration_seconds: float,
    reason: AgentRunSseCloseReason,
) -> None:
    _active_connections.add(-1)
    attributes = {"reason": reason}
    _connection_duration.record(duration_seconds, attributes=attributes)
    _connection_close.add(1, attributes=attributes)


def record_agent_run_sse_capacity_rejection(
    *,
    scope: AgentRunSseCapacityScope,
) -> None:
    _capacity_rejection.add(1, attributes={"scope": scope})


def record_agent_run_sse_projection_drop() -> None:
    _projection_drop.add(1, attributes={"reason": "unknown_event"})
