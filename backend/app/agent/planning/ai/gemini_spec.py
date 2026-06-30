"""Gemini question planner call spec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.agent.planning.ai.gemini_prompt import GeminiQuestionPlannerPrompt
from app.agent.planning.ai.schema_tool import QUESTION_PLANNER_GEMINI_SCHEMA
from app.analysis.prompt_versions import compute_call_signature
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule


@dataclass(frozen=True, slots=True)
class GeminiQuestionPlannerSpec:
    """Gemini question planner の 1 回の API call に必要な spec。"""

    provider: str
    model: str
    gen_config: Mapping[str, Any]
    structured_output: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    system_instruction: str | None
    version: str
    rate_limit_policy: AIModelRateLimitPolicy


_MODEL: Final[str] = "gemini-2.5-flash-lite"
_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "temperature": 0.1,
        "max_output_tokens": 1024,
    }
)
_STRUCTURED_OUTPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "response_mime_type": "application/json",
    }
)
_RESPONSE_SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(
    QUESTION_PLANNER_GEMINI_SCHEMA
)
_SYSTEM_INSTRUCTION: Final[str | None] = None
_VERSION: Final[str] = compute_call_signature(
    prompt_template=GeminiQuestionPlannerPrompt.TEMPLATE,
    model=_MODEL,
    gen_config={**_GEN_CONFIG, **_STRUCTURED_OUTPUT},
    response_schema=_RESPONSE_SCHEMA,
    system_instruction=_SYSTEM_INSTRUCTION,
)


GEMINI_QUESTION_PLANNER_SPEC: Final[GeminiQuestionPlannerSpec] = (
    GeminiQuestionPlannerSpec(
        provider="gemini",
        model=_MODEL,
        gen_config=_GEN_CONFIG,
        structured_output=_STRUCTURED_OUTPUT,
        response_schema=_RESPONSE_SCHEMA,
        system_instruction=_SYSTEM_INSTRUCTION,
        version=_VERSION,
        rate_limit_policy=AIModelRateLimitPolicy(
            provider="gemini",
            model=_MODEL,
            rules=(
                RateLimitRule(
                    name="rpd", max_requests=1500, window_seconds=86400, block=False
                ),
                RateLimitRule(
                    name="rpm", max_requests=100, window_seconds=60, block=True
                ),
            ),
        ),
    )
)
