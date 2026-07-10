"""Gemini direct answer call spec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.agent.answering.direct_answer.ai.prompt import GeminiDirectAnswerPrompt
from app.analysis.prompt_versions import compute_call_signature
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule


@dataclass(frozen=True, slots=True)
class GeminiDirectAnswerSpec:
    """Gemini direct answer の 1 回の API call に必要な spec。"""

    provider: str
    model: str
    gen_config: Mapping[str, Any]
    system_instruction: str | None
    version: str
    rate_limit_policy: AIModelRateLimitPolicy


_MODEL: Final[str] = "gemini-3.1-flash-lite"
_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "temperature": 0.2,
        "max_output_tokens": 2048,
    }
)
_SYSTEM_INSTRUCTION: Final[str | None] = None
_VERSION: Final[str] = compute_call_signature(
    prompt_template=GeminiDirectAnswerPrompt.TEMPLATE,
    model=_MODEL,
    gen_config=_GEN_CONFIG,
    response_schema=None,
    system_instruction=_SYSTEM_INSTRUCTION,
)


GEMINI_DIRECT_ANSWER_SPEC: Final[GeminiDirectAnswerSpec] = GeminiDirectAnswerSpec(
    provider="gemini",
    model=_MODEL,
    gen_config=_GEN_CONFIG,
    system_instruction=_SYSTEM_INSTRUCTION,
    version=_VERSION,
    rate_limit_policy=AIModelRateLimitPolicy(
        provider="gemini",
        model=_MODEL,
        rules=(
            RateLimitRule(
                name="rpd", max_requests=1500, window_seconds=86400, block=False
            ),
            RateLimitRule(name="rpm", max_requests=100, window_seconds=60, block=True),
        ),
    ),
)
