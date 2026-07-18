"""LLM Agent の宣言契約。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """Provider とモデルを識別する宣言。"""

    provider: str
    name: str


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """Provider-neutral な生成設定。"""

    temperature: float | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AgentPrompt[InputT]:
    """固定instructionsと実行時inputの変換を結ぶPrompt宣言。"""

    version: str
    instructions: str
    input_renderer: Callable[[InputT], str]


@dataclass(frozen=True, slots=True)
class Agent[InputT, OutputT]:
    """1つのLLM役割を表す不変の宣言。"""

    name: str
    prompt: AgentPrompt[InputT]
    model: ModelTarget
    model_settings: ModelSettings
    output_type: type[OutputT]
    response_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "response_schema", _freeze_schema(self.response_schema)
        )


def _freeze_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("response schema mapping keys must be strings")
        return MappingProxyType(
            {key: _freeze_schema(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_schema(item) for item in value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise TypeError(f"unsupported response schema value type: {type(value).__name__}")
