"""Stage 3 GeminiCurator の call spec を SSoT として保持する。

Prompt (本文 / sanitize / truncate) と Spec (API call config / version /
rate policy) を分離するための module。Spec は frozen dataclass + module
singleton で凍結し、Curator は ``SPEC`` class attr 経由でのみ参照する。

``version`` はハードコードせず ``compute_call_signature`` で算出する
(ADR ``docs/observability/pipeline-events-design.md`` §prompt_version の規律)。
TEMPLATE / model / gen_config / response_schema / system_instruction の入力が
変わらない限り出力は同じ 8 文字 hash で安定する。

Note: ``response_schema`` の Pydantic class 名は ``GeminiCurationResponse`` に
rename されたが ConfigDict の ``title="ExtractionResult"`` を据え置くことで
``model_json_schema()`` 出力上の title 値は不変 → ``_VERSION`` hash は連続。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel

from app.analysis.curation.ai.gemini_prompt import GeminiCurationPrompt
from app.analysis.curation.ai.schema import GeminiCurationResponse
from app.analysis.prompt_versions import compute_call_signature
from app.analysis.rate_limit import RatePolicy

_MODEL: Final[str] = "gemini-2.5-flash-lite"
_GEN_CONFIG: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "temperature": 0.2,
        "max_output_tokens": 2048,
        "response_mime_type": "application/json",
    }
)
_SYSTEM_INSTRUCTION: Final[str | None] = None
_VERSION: Final[str] = compute_call_signature(
    prompt_template=GeminiCurationPrompt.TEMPLATE,
    model=_MODEL,
    gen_config=_GEN_CONFIG,
    response_schema=GeminiCurationResponse.model_json_schema(),
    system_instruction=_SYSTEM_INSTRUCTION,
)


@dataclass(frozen=True, slots=True)
class GeminiCurationSpec:
    """Stage 3 GeminiCurator の 1 回の API call に必要な全 spec。

    Prompt 文面 (TEMPLATE) は分離し、本 Spec は ``provider`` / ``model`` /
    ``gen_config`` / ``response_schema`` / ``system_instruction`` /
    ``version`` / ``rate_policy`` のみを保持する。
    """

    provider: str
    model: str
    gen_config: Mapping[str, Any]
    response_schema: type[BaseModel]
    system_instruction: str | None
    version: str
    rate_policy: RatePolicy


GEMINI_CURATION_SPEC: Final[GeminiCurationSpec] = GeminiCurationSpec(
    provider="gemini",
    model=_MODEL,
    gen_config=_GEN_CONFIG,
    response_schema=GeminiCurationResponse,
    system_instruction=_SYSTEM_INSTRUCTION,
    version=_VERSION,
    rate_policy=RatePolicy(provider="gemini", model=_MODEL, rpm=100, rpd=1500),
)
