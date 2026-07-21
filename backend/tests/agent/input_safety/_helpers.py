"""Input Safety tests の未実装境界とruntime scope double。"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType, TracebackType
from typing import Any

import pytest

from tests.agent.runtime._fakes import ScriptedAgentRuntime


def required_input_safety_module(name: str) -> ModuleType:
    module_name = f"app.agent.input_safety.{name}"
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and exc.name.startswith("app.agent.input_safety"):
            pytest.fail(f"S2 input safety module is not implemented: {module_name}")
        raise


def required_input_safety_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"S2 input safety contract is missing: {module.__name__}.{name}")
    return getattr(module, name)


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
