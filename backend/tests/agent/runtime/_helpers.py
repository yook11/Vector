"""Agent runtime tests の固定入力と外部境界 fake。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import import_module
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget


class RuntimeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str
    tags: list[str]


class ValidationProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1)
    secret_number: int
    unsafe: str

    @field_validator("unsafe")
    @classmethod
    def _reject_unsafe_value(cls, value: str) -> str:
        raise ValueError("ARBITRARY_CTX_SENTINEL_7c62")


@dataclass(slots=True)
class FakeResponse:
    text: str | None
    candidates: list[Any] = field(default_factory=list)
    usage_metadata: object | None = None


class FakeGeminiClient:
    """外部 I/O 境界だけを差し替え、span は runtime の責務として生成しない。"""

    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.models = SimpleNamespace(
            generate_content=AsyncMock(side_effect=responses),
        )
        self.close = AsyncMock()
        self.aclose = AsyncMock()


def required_module(module_name: str) -> ModuleType:
    try:
        return import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"S2 runtime module is missing: {module_name} ({exc.name})")


def required_attribute(module: ModuleType, name: str) -> Any:
    if not hasattr(module, name):
        pytest.fail(f"S2 runtime contract is missing: {module.__name__}.{name}")
    return getattr(module, name)


def runtime_type() -> type[Any]:
    module = required_module("app.agent.runtime.gemini")
    return required_attribute(module, "GeminiAgentRuntime")


def runtime_contract() -> ModuleType:
    return required_module("app.agent.runtime.contract")


def make_agent(
    *,
    name: str = "runtime_probe",
    instructions: str = "SYSTEM_INSTRUCTIONS_SENTINEL_5f21",
    rendered_input: str = "TASK_CONTENTS_SENTINEL_8a43",
    model_name: str = "gemini-test-model",
    temperature: float | None = 0.25,
    max_output_tokens: int | None = 321,
    output_type: type[BaseModel] = RuntimeOutput,
) -> Agent[Any, Any]:
    return Agent(
        name=name,
        prompt=AgentPrompt(
            version="prompt-version-sentinel-v1",
            instructions=instructions,
            input_renderer=lambda _input: rendered_input,
        ),
        model=ModelTarget(provider="gemini", name=model_name),
        model_settings=ModelSettings(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
        output_type=output_type,
        response_schema={
            "type": "OBJECT",
            "required": ["result", "tags"],
            "properties": {
                "result": {"type": "STRING"},
                "tags": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                },
            },
        },
    )


def success_response(
    *,
    result: str = "accepted",
    tags: list[str] | None = None,
    usage_metadata: object | None = None,
) -> FakeResponse:
    return FakeResponse(
        text=json.dumps(
            {"result": result, "tags": ["runtime"] if tags is None else tags}
        ),
        usage_metadata=usage_metadata,
    )


def blocked_response(
    finish_reason_name: str,
    *,
    usage_metadata: object | None = None,
) -> FakeResponse:
    return FakeResponse(
        text="MODEL_OUTPUT_SENTINEL_BLOCKED_31d9",
        candidates=[
            SimpleNamespace(
                finish_reason=SimpleNamespace(name=finish_reason_name),
            )
        ],
        usage_metadata=usage_metadata,
    )
