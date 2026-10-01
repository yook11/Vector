"""``DEEPSEEK_ASSESSMENT_SPEC`` の構造を固定する golden table テスト。

Prompt と Spec を分離した結果として ``provider`` / ``model`` / ``version`` /
``gen_config`` (tuning) / ``structured_output`` (機構) / ``response_schema`` /
``system_instruction`` + DeepSeek 固有 ``tool_name`` /
``base_url`` が module singleton として SSoT に置かれていることを検証する。

``version`` は ``compute_call_signature`` で算出される 8 文字 hash。値は実効 call
config (gen_config + structured_output を含む) の deliberate な変更時のみ動くべきで、
機構を gen_config↔structured_output で移すだけの純粋リファクタでは動いてはならない。
そのため format (hex8) に加え、具体値を pin して意図しない回転を
検出する (一般則「実装出力を期待値にしない」の例外: opaque だが不変であるべき値の
characterization guard)。意図的 rotation 時は pin 値を更新し、audit 連続性 cutover の
意思表示を commit メッセージで残す (ADR §prompt_version の規律)。
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from app.analysis.assessment.ai.schema_tool import ASSESSMENT_TOOL_SCHEMA
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.analysis.assessment.domain.result import assessment_category_values

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def test_deepseek_provider_is_deepseek() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.provider == "deepseek"


def test_deepseek_model_is_v4_flash() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.model == "deepseek-v4-flash"


def test_deepseek_response_schema_equals_tool_schema() -> None:
    assert dict(DEEPSEEK_ASSESSMENT_SPEC.response_schema) == ASSESSMENT_TOOL_SCHEMA


def test_deepseek_category_enum_matches_assessment_category_values() -> None:
    assert ASSESSMENT_TOOL_SCHEMA["properties"]["category"]["enum"] == list(
        assessment_category_values()
    )


def test_deepseek_gen_config_is_immutable() -> None:
    assert isinstance(DEEPSEEK_ASSESSMENT_SPEC.gen_config, MappingProxyType)
    with pytest.raises(TypeError):
        DEEPSEEK_ASSESSMENT_SPEC.gen_config["max_tokens"] = 99  # type: ignore[index]


def test_deepseek_gen_config_has_tuning_fields_only() -> None:
    """gen_config は tuning のみ。機構は structured_output へ分離。"""
    assert DEEPSEEK_ASSESSMENT_SPEC.gen_config["max_tokens"] == 1536
    assert "tool_choice" not in DEEPSEEK_ASSESSMENT_SPEC.gen_config
    assert "extra_body" not in DEEPSEEK_ASSESSMENT_SPEC.gen_config


def test_deepseek_structured_output_is_immutable() -> None:
    assert isinstance(DEEPSEEK_ASSESSMENT_SPEC.structured_output, MappingProxyType)
    with pytest.raises(TypeError):
        DEEPSEEK_ASSESSMENT_SPEC.structured_output["tool_choice"] = {}  # type: ignore[index]


def test_deepseek_structured_output_includes_tool_choice_with_assess_article() -> None:
    tool_choice = DEEPSEEK_ASSESSMENT_SPEC.structured_output["tool_choice"]
    assert tool_choice["type"] == "function"
    assert tool_choice["function"]["name"] == "assess_article"


def test_deepseek_structured_output_disables_thinking() -> None:
    """DeepSeek Stage 4 は分類のみで reasoning trace 不要 (機構軸)。"""
    extra_body = DEEPSEEK_ASSESSMENT_SPEC.structured_output["extra_body"]
    assert extra_body["thinking"]["type"] == "disabled"


def test_deepseek_system_instruction_is_none() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.system_instruction is None


def test_deepseek_tool_name_is_assess_article() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.tool_name == "assess_article"


def test_deepseek_base_url_is_deepseek_beta() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.base_url == "https://api.deepseek.com/beta"


def test_deepseek_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        DEEPSEEK_ASSESSMENT_SPEC.tool_name = "other"  # type: ignore[misc]


def test_deepseek_tool_choice_matches_tool_name() -> None:
    """``structured_output["tool_choice"]["function"]["name"]`` と ``tool_name`` が
    spec 内で一致する内部 invariant を固定する。

    ``MappingProxyType`` は shallow freeze のため、nested ``tool_choice`` dict は
    外部から書換可能。両者がズレると DeepSeek の Function Calling が失敗するため、
    test で関係を pin する。
    """
    spec = DEEPSEEK_ASSESSMENT_SPEC
    assert spec.structured_output["tool_choice"]["function"]["name"] == spec.tool_name


# pre-D3-refactor (機構を gen_config から structured_output へ分離する前) に捕捉した
# version 値。機構の置き場所を移すだけの純粋リファクタでは hash は不変であるべきで、
# この pin が回転を検出する。意図的な prompt / 機構 rotation 時はこの値を更新し、
# commit メッセージで cutover を明示する (ADR §prompt_version の規律)。
_DEEPSEEK_PINNED_VERSION = "9e715824"


def test_deepseek_version_is_pinned() -> None:
    assert DEEPSEEK_ASSESSMENT_SPEC.version == _DEEPSEEK_PINNED_VERSION


def test_specs_versions_are_hex8() -> None:
    """8 文字 hex の format を将来 rotation 時の guard として固定する。"""
    assert _HEX8.fullmatch(DEEPSEEK_ASSESSMENT_SPEC.version) is not None


def test_response_schemas_have_no_topic_property() -> None:
    """topic は event-extraction 移行で完全削除済。Stage 4 schema に存在しない。"""
    deepseek = dict(DEEPSEEK_ASSESSMENT_SPEC.response_schema).get("properties", {})
    assert "topic" not in deepseek
