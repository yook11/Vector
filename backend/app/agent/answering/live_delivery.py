"""Best-effort live delivery helpers shared by answer generation flows."""

from __future__ import annotations

from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerGenerationContinuation,
    AnswerGenerationStopped,
)
from app.agent.runtime.contract import AgentTextStream

__all__ = [
    "BestEffortAnswerDeltaReporter",
    "close_answer_stream",
    "ensure_answer_generation_continues",
]


class BestEffortAnswerDeltaReporter:
    """Shield answer generation from missing reporters and delivery failures."""

    def __init__(self, inner: AnswerDeltaReporter | None) -> None:
        self._inner = inner

    async def append(self, *, generation: int, text: str) -> None:
        if self._inner is None:
            return
        try:
            await self._inner.append(generation=generation, text=text)
        except Exception:
            return

    async def reset(self, *, generation: int) -> None:
        if self._inner is None:
            return
        try:
            await self._inner.reset(generation=generation)
        except Exception:
            return

    async def finish(self, *, generation: int) -> None:
        if self._inner is None:
            return
        try:
            await self._inner.finish(generation=generation)
        except Exception:
            return

    async def abort(self, *, generation: int) -> None:
        if self._inner is None:
            return
        try:
            await self._inner.abort(generation=generation)
        except Exception:
            return


async def ensure_answer_generation_continues(
    continuation: AnswerGenerationContinuation | None,
) -> None:
    """Raise AnswerGenerationStopped when the continuation requests a stop."""

    if continuation is None:
        return
    if not await continuation.should_continue():
        raise AnswerGenerationStopped


async def close_answer_stream(stream: AgentTextStream | None) -> None:
    """Close a generator stream without letting cleanup failures escape."""

    if stream is None:
        return
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        return
