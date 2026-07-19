"""DeepSeekAgentRuntime tests の固定入力と provider boundary fake。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget


class RuntimeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str
    tags: list[str]


@dataclass(frozen=True, slots=True)
class DataclassRuntimeOutput:
    result: str
    tags: list[str]


class FakeDeepSeekClient:
    """外部 I/O 境界だけを差し替え、span は runtime の責務として生成しない。"""

    def __init__(self, responses: list[object | BaseException]) -> None:
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=responses))
        )
        self.close = AsyncMock()
        self.aclose = AsyncMock()


def required_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"PR2 runtime module is missing: {module_name} ({exc.name})")


def required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"PR2 runtime contract is missing: {module.__name__}.{name}")
    return getattr(module, name)


def runtime_contract() -> ModuleType:
    return required_module("app.agent.runtime.contract")


def runtime_type() -> type[Any]:
    return required_attribute(
        required_module("app.agent.runtime.deepseek"), "DeepSeekAgentRuntime"
    )


def binding_type() -> type[Any]:
    return required_attribute(
        required_module("app.agent.runtime.deepseek"), "DeepSeekOutputBinding"
    )


def make_binding() -> Any:
    return binding_type()(
        function_name="runtime_probe_output",
        description="Return the declared output object.",
    )


def make_agent(
    *,
    name: str = "deepseek_runtime_probe",
    instructions: str = "SYSTEM_INSTRUCTIONS_SENTINEL_8a23",
    rendered_input: str = "TASK_CONTENTS_SENTINEL_65ba",
    model_name: str = "deepseek-v4-flash",
    max_output_tokens: int | None = 321,
    output_type: type[Any] = RuntimeOutput,
) -> Agent[Any, Any]:
    return Agent(
        name=name,
        prompt=AgentPrompt(
            version="PROMPT_VERSION_SENTINEL_v1",
            instructions=instructions,
            input_renderer=lambda _input: rendered_input,
        ),
        model=ModelTarget(provider="deepseek", name=model_name),
        model_settings=ModelSettings(max_output_tokens=max_output_tokens),
        output_type=output_type,
        response_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["result", "tags"],
            "properties": {
                "result": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    )


def function_response(
    *,
    arguments: str | None = None,
    function_name: str = "runtime_probe_output",
    no_tool_calls: bool = False,
    usage: object | None = None,
    model: str | None = None,
) -> object:
    tool_calls = None
    if not no_tool_calls:
        tool_calls = [
            SimpleNamespace(
                function=SimpleNamespace(
                    name=function_name,
                    arguments=arguments if arguments is not None else "",
                )
            )
        ]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))],
        usage=usage,
        model=model,
    )


def success_response(
    *,
    result: str = "accepted",
    tags: list[str] | None = None,
    usage: object | None = None,
    model: str | None = None,
) -> object:
    return function_response(
        arguments=json.dumps({"result": result, "tags": tags or ["runtime"]}),
        usage=usage,
        model=model,
    )
