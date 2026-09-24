"""Gemini API キーの形式を種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import redact_gemini_api_keys

pytestmark = pytest.mark.unit

# 合成値を分割し、秘密検出ツールの規則に一致させない。
_GEMINI_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"


def test_gemini_key_is_replaced_with_context_kept() -> None:
    """Geminiキーを種別付きの表記に置き換え、周囲の原因文を残す。"""
    text = f"API key {_GEMINI_KEY} has been reported as leaked"
    assert redact_gemini_api_keys(text) == (
        "API key [redacted:gemini_api_key] has been reported as leaked"
    )


def test_gemini_key_in_query_is_replaced_with_other_parameters_kept() -> None:
    """URLクエリに入ったGeminiキーだけを置き換え、接続先と他のパラメーターを残す。"""
    text = (
        f"https://generativelanguage.googleapis.com/v1/models?key={_GEMINI_KEY}&alt=sse"
    )
    assert redact_gemini_api_keys(text) == (
        "https://generativelanguage.googleapis.com/v1/models"
        "?key=[redacted:gemini_api_key]&alt=sse"
    )


def test_every_gemini_key_is_replaced() -> None:
    """同じ文中の複数のGeminiキーを残さない。"""
    text = f"{_GEMINI_KEY} and {_GEMINI_KEY}"
    assert redact_gemini_api_keys(text) == (
        "[redacted:gemini_api_key] and [redacted:gemini_api_key]"
    )


def test_gemini_key_shorter_than_detection_length_is_preserved() -> None:
    """Geminiキー形式の長さに届かない文字列を部分置換しない。"""
    text = f"Error from {_GEMINI_KEY[:-1]}"
    assert redact_gemini_api_keys(text) == text
