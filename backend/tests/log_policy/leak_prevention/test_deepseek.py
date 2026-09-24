"""DeepSeek API キーの形式を種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import redact_deepseek_api_keys

pytestmark = pytest.mark.unit

# 合成値を分割し、秘密検出ツールの規則に一致させない。
_DEEPSEEK_KEY = "sk-" + "0123456789abcdef0123456789abcdef"


def test_deepseek_key_is_replaced_with_context_kept() -> None:
    """DeepSeekキーを種別付きの表記に置き換え、周囲の原因文を残す。"""
    text = f"Incorrect API key provided: {_DEEPSEEK_KEY}. retry later"
    assert redact_deepseek_api_keys(text) == (
        "Incorrect API key provided: [redacted:deepseek_api_key]. retry later"
    )


def test_deepseek_key_in_bearer_value_is_replaced() -> None:
    """Bearer に続くDeepSeekキーも置き換え、認証方式の語を残す。"""
    text = f"Bearer {_DEEPSEEK_KEY}"
    assert redact_deepseek_api_keys(text) == "Bearer [redacted:deepseek_api_key]"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_DEEPSEEK_KEY[:-1], id="too_short"),
        pytest.param(_DEEPSEEK_KEY + "0", id="too_long"),
        pytest.param("sk-" + "0123456789ABCDEF0123456789ABCDEF", id="uppercase_hex"),
        pytest.param("task-" + "0123456789abcdef0123456789abcdef", id="word_prefix"),
    ],
)
def test_text_outside_deepseek_key_boundary_is_preserved(text: str) -> None:
    """長さ・文字種・前後境界が一致しない識別子を部分置換しない。"""
    assert redact_deepseek_api_keys(text) == text
