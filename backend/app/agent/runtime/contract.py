"""Provider-neutral Agent runtime contracts."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Protocol

from app.agent.agent import Agent

__all__ = [
    "AgentResponseDefect",
    "AgentResponseInvalidError",
    "AgentRuntime",
    "AgentRuntimeScopeFactory",
]


class AgentRuntime(Protocol):
    """Agent宣言を検証済みoutputへ変換する1 attempt境界。"""

    async def invoke[InputT, OutputT](
        self,
        agent: Agent[InputT, OutputT],
        input: InputT,
        *,
        attempt_number: int,
    ) -> OutputT: ...


class AgentRuntimeScopeFactory(Protocol):
    """Provider resource scopeをRuntimeとして貸し出すfactory。"""

    def __call__(self) -> AbstractAsyncContextManager[AgentRuntime]: ...


class AgentResponseDefect(StrEnum):
    """Provider-neutralなstructured response違反。"""

    RESPONSE_NOT_JSON = "response_not_json"
    RESPONSE_NOT_OBJECT = "response_not_object"
    OUTPUT_SCHEMA_MISMATCH = "output_schema_mismatch"


class AgentResponseInvalidError(ValueError):
    """Agent outputを宣言されたPython契約へ変換できない。"""

    def __init__(
        self,
        defect: AgentResponseDefect,
        *,
        repair_hint: str | None = None,
    ) -> None:
        self.defect = defect
        self.repair_hint = repair_hint
        message = defect.value
        if repair_hint is not None:
            message = f"{message}: {repair_hint}"
        super().__init__(message)
