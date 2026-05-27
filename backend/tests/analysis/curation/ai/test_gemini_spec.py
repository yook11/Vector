"""``GeminiCurationSpec`` (singleton) の構造を固定する golden table テスト。

Prompt と Spec を分離した結果として ``provider`` / ``model`` / ``version`` /
``gen_config`` / ``response_schema`` / ``system_instruction`` / ``rate_limit_policy``
が module singleton として SSoT に置かれていることを検証する。

``version`` は ``compute_call_signature`` で算出される 8 文字 hash。具体値は
prompt 本文変更のたびに自動で動くため、テストでは値そのものを pin しない。
audit 連続性 cutover の意思表示は commit メッセージで行う
(ADR §prompt_version の規律)。
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from app.analysis.curation.ai.gemini_spec import GEMINI_CURATION_SPEC
from app.analysis.curation.ai.schema import GeminiCurationResponse
from app.analysis.rate_limit import AIModelRateLimitPolicy, RateLimitRule

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def test_provider_is_gemini() -> None:
    assert GEMINI_CURATION_SPEC.provider == "gemini"


def test_model_is_flash_lite_25() -> None:
    assert GEMINI_CURATION_SPEC.model == "gemini-2.5-flash-lite"


def test_response_schema_is_gemini_extraction_response() -> None:
    assert GEMINI_CURATION_SPEC.response_schema is GeminiCurationResponse


def test_gen_config_is_mapping_proxy_and_immutable() -> None:
    assert isinstance(GEMINI_CURATION_SPEC.gen_config, MappingProxyType)
    with pytest.raises(TypeError):
        GEMINI_CURATION_SPEC.gen_config["temperature"] = 0.5  # type: ignore[index]


def test_gen_config_has_required_fields() -> None:
    assert GEMINI_CURATION_SPEC.gen_config["temperature"] == 0.2
    assert GEMINI_CURATION_SPEC.gen_config["max_output_tokens"] == 2048
    assert GEMINI_CURATION_SPEC.gen_config["response_mime_type"] == "application/json"


def test_system_instruction_is_none() -> None:
    """将来 prompt rotation で変えやすいよう golden 化。"""
    assert GEMINI_CURATION_SPEC.system_instruction is None


def test_rate_limit_policy_is_provider_model_rules() -> None:
    assert GEMINI_CURATION_SPEC.rate_limit_policy == AIModelRateLimitPolicy(
        provider="gemini",
        model="gemini-2.5-flash-lite",
        rules=(
            RateLimitRule(
                name="rpd", max_requests=1500, window_seconds=86400, block=False
            ),
            RateLimitRule(name="rpm", max_requests=100, window_seconds=60, block=True),
        ),
    )


def test_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        GEMINI_CURATION_SPEC.provider = "openai"  # type: ignore[misc]


def test_version_is_hex8() -> None:
    """8 文字 hex の format を将来 rotation 時の guard として固定する。"""
    assert _HEX8.fullmatch(GEMINI_CURATION_SPEC.version) is not None
