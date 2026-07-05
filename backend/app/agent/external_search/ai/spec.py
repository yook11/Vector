"""DeepSeek call specs for external search LLM ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.agent.external_search.ai.prompts import (
    EXTERNAL_EVIDENCE_SELECTOR_PROMPT,
    EXTERNAL_QUERY_GENERATOR_PROMPT,
)
from app.agent.external_search.ai.schema_tool import (
    EVIDENCE_SELECTOR_TOOL_SCHEMA,
    QUERY_GENERATOR_TOOL_SCHEMA,
)
from app.analysis.prompt_versions import compute_call_signature
from app.analysis.rate_limit import AIModelRateLimitPolicy

EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS: Final[int] = 20

_DEEPSEEK_MODEL: Final[str] = "deepseek-v4-flash"
_DEEPSEEK_BASE_URL: Final[str] = "https://api.deepseek.com/beta"
_QUERY_GENERATOR_TOOL_NAME: Final[str] = "generate_search_queries"
_EVIDENCE_SELECTOR_TOOL_NAME: Final[str] = "select_evidence"
_SYSTEM_INSTRUCTION: Final[str | None] = None


@dataclass(frozen=True, slots=True)
class ExternalSearchDeepSeekSpec:
    """DeepSeek Function Calling に必要な external search call spec。"""

    provider: str
    model: str
    gen_config: Mapping[str, Any]
    structured_output: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    prompt_template: str
    system_instruction: str | None
    version: str
    rate_limit_policy: AIModelRateLimitPolicy
    tool_name: str
    base_url: str
    timeout_seconds: int


def _structured_output(tool_name: str) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "tool_choice": {
                "type": "function",
                "function": {"name": tool_name},
            },
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    )


def _version(
    *,
    prompt_template: str,
    gen_config: Mapping[str, Any],
    structured_output: Mapping[str, Any],
    response_schema: Mapping[str, Any],
) -> str:
    return compute_call_signature(
        prompt_template=prompt_template,
        model=_DEEPSEEK_MODEL,
        gen_config={**gen_config, **structured_output},
        response_schema=response_schema,
        system_instruction=_SYSTEM_INSTRUCTION,
    )


_QUERY_GENERATOR_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {"max_tokens": 256}
)
_QUERY_GENERATOR_STRUCTURED_OUTPUT: Final[Mapping[str, Any]] = _structured_output(
    _QUERY_GENERATOR_TOOL_NAME
)
_QUERY_GENERATOR_RESPONSE_SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(
    QUERY_GENERATOR_TOOL_SCHEMA
)

DEEPSEEK_QUERY_GENERATOR_SPEC: Final[ExternalSearchDeepSeekSpec] = (
    ExternalSearchDeepSeekSpec(
        provider="deepseek",
        model=_DEEPSEEK_MODEL,
        gen_config=_QUERY_GENERATOR_GEN_CONFIG,
        structured_output=_QUERY_GENERATOR_STRUCTURED_OUTPUT,
        response_schema=_QUERY_GENERATOR_RESPONSE_SCHEMA,
        prompt_template=EXTERNAL_QUERY_GENERATOR_PROMPT,
        system_instruction=_SYSTEM_INSTRUCTION,
        version=_version(
            prompt_template=EXTERNAL_QUERY_GENERATOR_PROMPT,
            gen_config=_QUERY_GENERATOR_GEN_CONFIG,
            structured_output=_QUERY_GENERATOR_STRUCTURED_OUTPUT,
            response_schema=_QUERY_GENERATOR_RESPONSE_SCHEMA,
        ),
        rate_limit_policy=AIModelRateLimitPolicy(
            provider="deepseek",
            model=_DEEPSEEK_MODEL,
            rules=(),
        ),
        tool_name=_QUERY_GENERATOR_TOOL_NAME,
        base_url=_DEEPSEEK_BASE_URL,
        timeout_seconds=EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
    )
)

_EVIDENCE_SELECTOR_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {"max_tokens": 2048}
)
_EVIDENCE_SELECTOR_STRUCTURED_OUTPUT: Final[Mapping[str, Any]] = _structured_output(
    _EVIDENCE_SELECTOR_TOOL_NAME
)
_EVIDENCE_SELECTOR_RESPONSE_SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(
    EVIDENCE_SELECTOR_TOOL_SCHEMA
)

DEEPSEEK_EVIDENCE_SELECTOR_SPEC: Final[ExternalSearchDeepSeekSpec] = (
    ExternalSearchDeepSeekSpec(
        provider="deepseek",
        model=_DEEPSEEK_MODEL,
        gen_config=_EVIDENCE_SELECTOR_GEN_CONFIG,
        structured_output=_EVIDENCE_SELECTOR_STRUCTURED_OUTPUT,
        response_schema=_EVIDENCE_SELECTOR_RESPONSE_SCHEMA,
        prompt_template=EXTERNAL_EVIDENCE_SELECTOR_PROMPT,
        system_instruction=_SYSTEM_INSTRUCTION,
        version=_version(
            prompt_template=EXTERNAL_EVIDENCE_SELECTOR_PROMPT,
            gen_config=_EVIDENCE_SELECTOR_GEN_CONFIG,
            structured_output=_EVIDENCE_SELECTOR_STRUCTURED_OUTPUT,
            response_schema=_EVIDENCE_SELECTOR_RESPONSE_SCHEMA,
        ),
        rate_limit_policy=AIModelRateLimitPolicy(
            provider="deepseek",
            model=_DEEPSEEK_MODEL,
            rules=(),
        ),
        tool_name=_EVIDENCE_SELECTOR_TOOL_NAME,
        base_url=_DEEPSEEK_BASE_URL,
        timeout_seconds=EXTERNAL_DEEPSEEK_TIMEOUT_SECONDS,
    )
)
