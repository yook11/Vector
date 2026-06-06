"""Stage 4 Assessor 群の call spec を SSoT として保持する。

Prompt (本文 / sanitize / truncate) と Spec (API call config / version /
rate policy / DeepSeek 固有の tool_name / base_url) を分離する。Spec は
frozen dataclass + module singleton で凍結し、Assessor は ``SPEC`` class attr
経由でのみ参照する。

``gen_config`` は task 軸の tuning (temperature 等)、``structured_output`` は
provider 軸の構造化出力強制機構 (Gemini JSON mode / DeepSeek Function Calling) で、
別軸として分離する。

``version`` はハードコードせず ``compute_call_signature`` で算出する
(ADR ``docs/observability/pipeline-events-design.md`` §prompt_version の規律)。
hash 入力の gen_config は実効 config ``{**gen_config, **structured_output}`` を渡す
(tuning と機構の置き場所を分けても hash は不変、機構そのものを変えれば hash は回る)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from app.analysis.assessment.ai.prompts import ASSESSMENT_PROMPT
from app.analysis.assessment.ai.schema_tool import (
    ASSESSMENT_GEMINI_SCHEMA,
    ASSESSMENT_TOOL_SCHEMA,
)
from app.analysis.prompt_versions import compute_call_signature
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule


@dataclass(frozen=True, slots=True)
class AssessmentCallSpec:
    """Stage 4 Assessor の 1 回の API call に必要な共通 spec。

    ``gen_config`` は task 軸の生成 tuning、``structured_output`` は provider 軸の
    構造化出力強制機構。adapter は両者を SDK call に splat する。
    """

    provider: str
    model: str
    gen_config: Mapping[str, Any]
    structured_output: Mapping[str, Any]
    response_schema: Mapping[str, Any]
    system_instruction: str | None
    version: str
    rate_limit_policy: AIModelRateLimitPolicy


@dataclass(frozen=True, slots=True)
class DeepSeekAssessmentSpec(AssessmentCallSpec):
    """DeepSeek 固有の Function Calling 設定 + 接続 endpoint を加えた spec。

    - ``tool_name``: Function Calling の関数名 (tool_choice + tools.function.name
      で参照、prompt 本文の概念ではなく call config なので Spec 側に置く)。
    - ``base_url``: OpenAI SDK 共用のための ``AsyncOpenAI(base_url=...)`` 値。
    """

    tool_name: str
    base_url: str


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

_GEMINI_MODEL: Final[str] = "gemini-2.5-flash-lite"
_GEMINI_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "temperature": 0.2,
        "max_output_tokens": 1024,
    }
)
# Gemini で構造化出力を強制する機構 (JSON mode)。provider 固有・tuning と別軸。
_GEMINI_STRUCTURED_OUTPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "response_mime_type": "application/json",
    }
)
_GEMINI_RESPONSE_SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(
    ASSESSMENT_GEMINI_SCHEMA
)
_GEMINI_SYSTEM_INSTRUCTION: Final[str | None] = None
_GEMINI_VERSION: Final[str] = compute_call_signature(
    prompt_template=ASSESSMENT_PROMPT,
    model=_GEMINI_MODEL,
    gen_config={**_GEMINI_GEN_CONFIG, **_GEMINI_STRUCTURED_OUTPUT},
    response_schema=_GEMINI_RESPONSE_SCHEMA,
    system_instruction=_GEMINI_SYSTEM_INSTRUCTION,
)

GEMINI_ASSESSMENT_SPEC: Final[AssessmentCallSpec] = AssessmentCallSpec(
    provider="gemini",
    model=_GEMINI_MODEL,
    gen_config=_GEMINI_GEN_CONFIG,
    structured_output=_GEMINI_STRUCTURED_OUTPUT,
    response_schema=_GEMINI_RESPONSE_SCHEMA,
    system_instruction=_GEMINI_SYSTEM_INSTRUCTION,
    version=_GEMINI_VERSION,
    rate_limit_policy=AIModelRateLimitPolicy(
        provider="gemini",
        model=_GEMINI_MODEL,
        rules=(
            RateLimitRule(
                name="rpd", max_requests=1500, window_seconds=86400, block=False
            ),
            RateLimitRule(name="rpm", max_requests=100, window_seconds=60, block=True),
        ),
    ),
)

# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------

_DEEPSEEK_MODEL: Final[str] = "deepseek-v4-flash"
_DEEPSEEK_TOOL_NAME: Final[str] = "assess_article"
_DEEPSEEK_BASE_URL: Final[str] = "https://api.deepseek.com/beta"
_DEEPSEEK_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "max_tokens": 512,
    }
)
# DeepSeek で構造化出力を強制する機構 (Function Calling + thinking 無効)。
# tool_choice が forced tool、thinking 無効で reasoning trace を出さず envelope を
# 確定させる。両者で 1 個のクリーンな構造化出力を強制する provider 固有軸。
_DEEPSEEK_STRUCTURED_OUTPUT: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "tool_choice": {
            "type": "function",
            "function": {"name": _DEEPSEEK_TOOL_NAME},
        },
        "extra_body": {"thinking": {"type": "disabled"}},
    }
)
_DEEPSEEK_RESPONSE_SCHEMA: Final[Mapping[str, Any]] = MappingProxyType(
    ASSESSMENT_TOOL_SCHEMA
)
_DEEPSEEK_SYSTEM_INSTRUCTION: Final[str | None] = None
_DEEPSEEK_VERSION: Final[str] = compute_call_signature(
    prompt_template=ASSESSMENT_PROMPT,
    model=_DEEPSEEK_MODEL,
    gen_config={**_DEEPSEEK_GEN_CONFIG, **_DEEPSEEK_STRUCTURED_OUTPUT},
    response_schema=_DEEPSEEK_RESPONSE_SCHEMA,
    system_instruction=_DEEPSEEK_SYSTEM_INSTRUCTION,
)

DEEPSEEK_ASSESSMENT_SPEC: Final[DeepSeekAssessmentSpec] = DeepSeekAssessmentSpec(
    provider="deepseek",
    model=_DEEPSEEK_MODEL,
    gen_config=_DEEPSEEK_GEN_CONFIG,
    structured_output=_DEEPSEEK_STRUCTURED_OUTPUT,
    response_schema=_DEEPSEEK_RESPONSE_SCHEMA,
    system_instruction=_DEEPSEEK_SYSTEM_INSTRUCTION,
    version=_DEEPSEEK_VERSION,
    rate_limit_policy=AIModelRateLimitPolicy(
        provider="deepseek", model=_DEEPSEEK_MODEL, rules=()
    ),
    tool_name=_DEEPSEEK_TOOL_NAME,
    base_url=_DEEPSEEK_BASE_URL,
)
