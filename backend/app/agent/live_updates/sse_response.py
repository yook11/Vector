"""HTTP response ownership for agent run SSE capacity leases."""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.agent.live_updates.sse import AgentRunSseCapacityLease


class AgentRunSseStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        lease: AgentRunSseCapacityLease,
        media_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._lease.release()
