"""``GEMINI_ASSESSMENT_SPEC`` / ``DEEPSEEK_ASSESSMENT_SPEC`` の構造を固定する
golden table テスト。

Prompt と Spec を分離した結果として ``provider`` / ``model`` / ``version`` /
``gen_config`` / ``response_schema`` / ``system_instruction`` / ``rate_policy``
+ DeepSeek 固有 ``tool_name`` / ``base_url`` が module singleton として SSoT に
置かれていることを検証する。

``version`` は ``compute_call_signature`` で算出される 8 文字 hash。
本 PR (配置換え) では入力 5 要素が変わらないため golden Gemini ``"e1a5fdb8"`` /
DeepSeek ``"cdde3632"`` を維持する。意図的な prompt / schema 変更時のみこの値を
更新し、commit メッセージで audit 連続性 cutover を明示する。
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from app.analysis.assessment.ai.schema_tool import (
    ASSESSMENT_GEMINI_SCHEMA,
    ASSESSMENT_TOOL_SCHEMA,
)
from app.analysis.assessment.ai.spec import (
    DEEPSEEK_ASSESSMENT_SPEC,
    GEMINI_ASSESSMENT_SPEC,
)
from app.analysis.rate_policy import RatePolicy

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_provider_is_gemini() -> None:
    assert GEMINI_ASSESSMENT_SPEC.provider == "gemini"


def test_gemini_model_is_flash_lite_25() -> None:
    assert GEMINI_ASSESSMENT_SPEC.model == "gemini-2.5-flash-lite"


def test_gemini_version_locked() -> None:
    """配置換えで version 値が変わらない golden 固定。

    入力 (TEMPLATE / model / gen_config / response_schema / system_instruction)
    のいずれかが変わったらこの値も変わる。意図的変更でない場合は配置換え以外の
    差分が混入したサイン。
    """
    assert GEMINI_ASSESSMENT_SPEC.version == "e1a5fdb8"


def test_gemini_response_schema_equals_gemini_schema() -> None:
    assert dict(GEMINI_ASSESSMENT_SPEC.response_schema) == ASSESSMENT_GEMINI_SCHEMA


def test_gemini_gen_config_is_mapping_proxy_and_immutable() -> None:
    assert isinstance(GEMINI_ASSESSMENT_SPEC.gen_config, MappingProxyType)
    with pytest.raises(TypeError):
        GEMINI_ASSESSMENT_SPEC.gen_config["temperature"] = 0.5  # type: ignore[index]


def test_gemini_gen_config_has_required_fields() -> None:
    assert GEMINI_ASSESSMENT_SPEC.gen_config["temperature"] == 0.2
    assert GEMINI_ASSESSMENT_SPEC.gen_config["max_output_tokens"] == 1024
    assert GEMINI_ASSESSMENT_SPEC.gen_config["response_mime_type"] == "application/json"


def test_gemini_system_instruction_is_none() -> None:
    """将来 prompt rotation で変えやすいよう golden 化。"""
    assert GEMINI_ASSESSMENT_SPEC.system_instruction is None


def test_gemini_rate_policy_equals_provider_model_rpm_rpd() -> None:
    assert GEMINI_ASSESSMENT_SPEC.rate_policy == RatePolicy(
        provider="gemini",
        model="gemini-2.5-flash-lite",
        rpm=100,
        rpd=1500,
    )


def test_gemini_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        GEMINI_ASSESSMENT_SPEC.provider = "openai"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------


def test_deepseek_provider_is_deepseek() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.provider == "deepseek"


def test_deepseek_model_is_v4_flash() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.model == "deepseek-v4-flash"


def test_deepseek_version_locked() -> None:
    """配置換えで version 値が変わらない golden 固定。"""
    assert DEEPSEEK_ASSESSMENT_SPEC.version == "cdde3632"


def test_deepseek_response_schema_equals_tool_schema() -> None:
    assert dict(DEEPSEEK_ASSESSMENT_SPEC.response_schema) == ASSESSMENT_TOOL_SCHEMA


def test_deepseek_gen_config_is_immutable() -> None:
    assert isinstance(DEEPSEEK_ASSESSMENT_SPEC.gen_config, MappingProxyType)
    with pytest.raises(TypeError):
        DEEPSEEK_ASSESSMENT_SPEC.gen_config["max_tokens"] = 99  # type: ignore[index]


def test_deepseek_gen_config_includes_tool_choice_with_assess_article() -> None:
    tool_choice = DEEPSEEK_ASSESSMENT_SPEC.gen_config["tool_choice"]
    assert tool_choice["type"] == "function"
    assert tool_choice["function"]["name"] == "assess_article"


def test_deepseek_gen_config_disables_thinking() -> None:
    """DeepSeek Stage 4 は分類のみで reasoning trace 不要。"""
    extra_body = DEEPSEEK_ASSESSMENT_SPEC.gen_config["extra_body"]
    assert extra_body["thinking"]["type"] == "disabled"


def test_deepseek_system_instruction_is_none() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.system_instruction is None


def test_deepseek_rate_policy_has_none_rpm_rpd() -> None:
    """DeepSeek は公式 RPM/RPD 公開なし、429 は OpenAI SDK retry に任せる方針。"""
    assert DEEPSEEK_ASSESSMENT_SPEC.rate_policy == RatePolicy(
        provider="deepseek",
        model="deepseek-v4-flash",
        rpm=None,
        rpd=None,
    )


def test_deepseek_tool_name_is_assess_article() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.tool_name == "assess_article"


def test_deepseek_base_url_is_deepseek_beta() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.base_url == "https://api.deepseek.com/beta"


def test_deepseek_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        DEEPSEEK_ASSESSMENT_SPEC.tool_name = "other"  # type: ignore[misc]


def test_deepseek_tool_choice_matches_tool_name() -> None:
    """``gen_config["tool_choice"]["function"]["name"]`` と ``tool_name`` が
    spec 内で一致する内部 invariant を固定する。

    ``MappingProxyType`` は shallow freeze のため、nested ``tool_choice`` dict は
    外部から書換可能。両者がズレると DeepSeek の Function Calling が失敗するため、
    test で関係を pin する。
    """
    spec = DEEPSEEK_ASSESSMENT_SPEC
    assert spec.gen_config["tool_choice"]["function"]["name"] == spec.tool_name


# ---------------------------------------------------------------------------
# 横断
# ---------------------------------------------------------------------------


def test_specs_have_distinct_versions() -> None:
    """model + gen_config + schema が違うので hash も別物。"""
    assert GEMINI_ASSESSMENT_SPEC.version != DEEPSEEK_ASSESSMENT_SPEC.version


def test_specs_versions_are_hex8() -> None:
    """8 文字 hex の format を将来 rotation 時の guard として固定する。"""
    assert _HEX8.fullmatch(GEMINI_ASSESSMENT_SPEC.version) is not None
    assert _HEX8.fullmatch(DEEPSEEK_ASSESSMENT_SPEC.version) is not None
