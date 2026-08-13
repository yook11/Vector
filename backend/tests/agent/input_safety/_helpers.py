"""Input Safety tests のruntime scope double。"""

from __future__ import annotations

from types import TracebackType

from tests.agent.runtime._fakes import ScriptedAgentRuntime


class RuntimeScope:
    def __init__(
        self,
        factory: RecordingRuntimeScopeFactory,
        runtime: ScriptedAgentRuntime,
    ) -> None:
        self._factory = factory
        self._runtime = runtime

    async def __aenter__(self) -> ScriptedAgentRuntime:
        self._factory.entered += 1
        return self._runtime

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._factory.exits.append((exc_type, exc, traceback))
        return False


class RecordingRuntimeScopeFactory:
    def __init__(self, runtime: ScriptedAgentRuntime) -> None:
        self._runtime = runtime
        self.created = 0
        self.entered = 0
        self.exits: list[
            tuple[
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ]
        ] = []

    def __call__(self) -> RuntimeScope:
        self.created += 1
        return RuntimeScope(self, self._runtime)
