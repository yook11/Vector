"""Logfire write token の形式を種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import redact_logfire_tokens

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("region", ["us", "eu"])
def test_logfire_token_is_replaced_with_context_kept(region: str) -> None:
    """地域によらずLogfireトークンを種別付きの表記に置き換え、周囲の原因文を残す。"""
    # 合成値を分割し、秘密検出ツールの規則に一致させない。
    token = f"pylf_v1_{region}_" + "SyntheticToken0123456789"
    assert redact_logfire_tokens(f"export failed with {token} status=401") == (
        "export failed with [redacted:logfire_token] status=401"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("pylf_v2_us_SyntheticToken0123456789", id="other_version"),
        pytest.param("pylf_v1_usa_SyntheticToken0123456789", id="three_letter_region"),
        pytest.param("pylf_v1_us_", id="empty_body"),
    ],
)
def test_text_outside_logfire_token_format_is_preserved(text: str) -> None:
    """版・地域・本体の形式が一致しない文字列を置換しない。"""
    assert redact_logfire_tokens(text) == text
