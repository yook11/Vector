"""``GEMINI_ASSESSMENT_SPEC`` の構造を固定する golden table テスト。

Prompt と Spec を分離した結果として ``provider`` / ``model`` / ``version`` /
``gen_config`` / ``response_schema`` / ``system_instruction`` が module singleton
として SSoT に置かれていることを検証する。

``version`` は ``compute_call_signature`` で算出される 8 文字 hash。値は call config の
deliberate な変更時のみ動くべきなので、format (hex8) に加え具体値を pin して意図しない
回転を検出する (一般則「実装出力を期待値にしない」の例外: opaque だが不変であるべき値の
characterization guard)。意図的 rotation 時は pin 値を更新し、audit 連続性 cutover の
意思表示を commit メッセージで残す (ADR §prompt_version の規律)。
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from app.analysis.assessment.ai.schema_tool import ASSESSMENT_GEMINI_SCHEMA
from app.analysis.assessment.ai.spec import GEMINI_ASSESSMENT_SPEC
from app.analysis.assessment.domain.result import assessment_category_values

_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def test_provider_is_gemini() -> None:
    assert GEMINI_ASSESSMENT_SPEC.provider == "gemini"


def test_model_is_flash_lite_35() -> None:
    assert GEMINI_ASSESSMENT_SPEC.model == "gemini-3.5-flash-lite"


def test_response_schema_equals_gemini_schema() -> None:
    assert dict(GEMINI_ASSESSMENT_SPEC.response_schema) == ASSESSMENT_GEMINI_SCHEMA


def test_category_enum_matches_assessment_category_values() -> None:
    assert ASSESSMENT_GEMINI_SCHEMA["properties"]["category"]["enum"] == list(
        assessment_category_values()
    )


def test_gen_config_is_immutable() -> None:
    assert isinstance(GEMINI_ASSESSMENT_SPEC.gen_config, MappingProxyType)
    with pytest.raises(TypeError):
        GEMINI_ASSESSMENT_SPEC.gen_config["max_output_tokens"] = 99  # type: ignore[index]


def test_gen_config_leaves_temperature_to_the_gemini_3_default() -> None:
    assert dict(GEMINI_ASSESSMENT_SPEC.gen_config) == {
        "max_output_tokens": 4096,
        "response_mime_type": "application/json",
    }


def test_system_instruction_is_none() -> None:
    assert GEMINI_ASSESSMENT_SPEC.system_instruction is None


def test_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        GEMINI_ASSESSMENT_SPEC.model = "other"  # type: ignore[misc]


# DeepSeek から gemini-3.5-flash-lite への切り替え (モデル・生成設定・schema の変更) で
# 意図的に回転させた値。
_PINNED_VERSION = "22cb52d2"


def test_version_is_pinned() -> None:
    assert GEMINI_ASSESSMENT_SPEC.version == _PINNED_VERSION


def test_version_is_hex8() -> None:
    """8 文字 hex の format を将来 rotation 時の guard として固定する。"""
    assert _HEX8.fullmatch(GEMINI_ASSESSMENT_SPEC.version) is not None


def test_response_schema_has_no_topic_property() -> None:
    """topic は event-extraction 移行で完全削除済。Stage 4 schema に存在しない。"""
    properties = dict(GEMINI_ASSESSMENT_SPEC.response_schema).get("properties", {})
    assert "topic" not in properties
