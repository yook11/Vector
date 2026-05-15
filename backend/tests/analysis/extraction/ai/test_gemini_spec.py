"""``GeminiExtractionSpec`` (singleton) の構造を固定する golden table テスト。

Prompt と Spec を分離した結果として ``provider`` / ``model`` / ``version`` /
``gen_config`` / ``response_schema`` / ``system_instruction`` / ``rate_policy``
が module singleton として SSoT に置かれていることを検証する。

``version`` は ``compute_call_signature`` で算出される 8 文字 hash。
本 PR (配置換え) では入力 5 要素が変わらないため golden ``"9ff9f0cf"`` を維持する。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from app.analysis.extraction.ai.gemini_spec import GEMINI_EXTRACTION_SPEC
from app.analysis.extraction.ai.schema import GeminiExtractionResponse
from app.analysis.rate_limit import RatePolicy


def test_provider_is_gemini() -> None:
    assert GEMINI_EXTRACTION_SPEC.provider == "gemini"


def test_model_is_flash_lite_25() -> None:
    assert GEMINI_EXTRACTION_SPEC.model == "gemini-2.5-flash-lite"


def test_version_locked() -> None:
    """配置換えで version 値が変わらない golden 固定。

    event-extraction PR 2 で entities フィールドと prompt の対応行を削除した
    意図的 cutover により ``"094404f1"`` に更新。意図的な prompt / schema
    変更時のみこの値を更新し、commit メッセージで audit 連続性 cutover を明示する。
    """
    assert GEMINI_EXTRACTION_SPEC.version == "094404f1"


def test_response_schema_is_gemini_extraction_response() -> None:
    assert GEMINI_EXTRACTION_SPEC.response_schema is GeminiExtractionResponse


def test_gen_config_is_mapping_proxy_and_immutable() -> None:
    assert isinstance(GEMINI_EXTRACTION_SPEC.gen_config, MappingProxyType)
    with pytest.raises(TypeError):
        GEMINI_EXTRACTION_SPEC.gen_config["temperature"] = 0.5  # type: ignore[index]


def test_gen_config_has_required_fields() -> None:
    assert GEMINI_EXTRACTION_SPEC.gen_config["temperature"] == 0.2
    assert GEMINI_EXTRACTION_SPEC.gen_config["max_output_tokens"] == 2048
    assert GEMINI_EXTRACTION_SPEC.gen_config["response_mime_type"] == "application/json"


def test_system_instruction_is_none() -> None:
    """将来 prompt rotation で変えやすいよう golden 化。"""
    assert GEMINI_EXTRACTION_SPEC.system_instruction is None


def test_rate_policy_is_provider_model_rpm_rpd() -> None:
    assert GEMINI_EXTRACTION_SPEC.rate_policy == RatePolicy(
        provider="gemini",
        model="gemini-2.5-flash-lite",
        rpm=100,
        rpd=1500,
    )


def test_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        GEMINI_EXTRACTION_SPEC.provider = "openai"  # type: ignore[misc]
