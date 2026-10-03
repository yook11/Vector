"""Stage 4 GeminiAssessor の call spec を SSoT として保持する。

Prompt (本文 / sanitize / truncate) と Spec (API call config / version) を分離する。
Spec は frozen dataclass + module singleton で凍結し、Assessor は ``SPEC`` class attr
経由でのみ参照する。

``version`` はハードコードせず ``compute_call_signature`` で算出する
(ADR ``docs/observability/pipeline-events-design.md`` §prompt_version の規律)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.analysis.assessment.ai.prompts import ASSESSMENT_PROMPT
from app.analysis.assessment.ai.schema_tool import ASSESSMENT_GEMINI_SCHEMA
from app.analysis.prompt_versions import compute_call_signature


@dataclass(frozen=True, slots=True)
class GeminiAssessmentSpec:
    """Stage 4 GeminiAssessor の 1 回の API call に必要な全 spec。"""

    provider: str
    model: str
    gen_config: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    system_instruction: str | None
    version: str


_MODEL: Final[str] = "gemini-3.5-flash-lite"
# thinking (既定 minimal) も出力上限に含まれるため、key_points の出力に余裕を持たせる。
_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "max_output_tokens": 4096,
        "response_mime_type": "application/json",
    }
)
_RESPONSE_SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(ASSESSMENT_GEMINI_SCHEMA)
_SYSTEM_INSTRUCTION: Final[str | None] = None
_VERSION: Final[str] = compute_call_signature(
    prompt_template=ASSESSMENT_PROMPT,
    model=_MODEL,
    gen_config=_GEN_CONFIG,
    response_schema=_RESPONSE_SCHEMA,
    system_instruction=_SYSTEM_INSTRUCTION,
)

GEMINI_ASSESSMENT_SPEC: Final[GeminiAssessmentSpec] = GeminiAssessmentSpec(
    provider="gemini",
    model=_MODEL,
    gen_config=_GEN_CONFIG,
    response_schema=_RESPONSE_SCHEMA,
    system_instruction=_SYSTEM_INSTRUCTION,
    version=_VERSION,
)
