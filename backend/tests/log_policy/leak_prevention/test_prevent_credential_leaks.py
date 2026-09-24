"""漏洩防止の入口が、既知の種類の認証情報をすべて置き換え、通常の文を変えない契約。"""

from __future__ import annotations

import pytest

from app.log_policy.budget import TEXT_LIMIT
from app.log_policy.leak_prevention import prevent_credential_leaks

pytestmark = pytest.mark.unit

# 合成値を分割し、秘密検出ツールの規則に一致させない。
_PEM_BLOCK = (
    "-----BEGIN "
    + "PRIVATE KEY-----\nsynthetic-private-body\n-----END PRIVATE KEY-----"
)
_GEMINI_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
_DEEPSEEK_KEY = "sk-" + "0123456789abcdef0123456789abcdef"
_LOGFIRE_TOKEN = "pylf_v1_us_" + "SyntheticToken0123456789"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            f"failed {_PEM_BLOCK}", "failed [redacted:private_key]", id="private_key"
        ),
        pytest.param(
            f"key {_GEMINI_KEY} leaked",
            "key [redacted:gemini_api_key] leaked",
            id="gemini_api_key",
        ),
        pytest.param(
            f"key {_DEEPSEEK_KEY} rejected",
            "key [redacted:deepseek_api_key] rejected",
            id="deepseek_api_key",
        ),
        pytest.param(
            f"token {_LOGFIRE_TOKEN} rejected",
            "token [redacted:logfire_token] rejected",
            id="logfire_token",
        ),
        pytest.param(
            "for AKIAIOSFODNN7EXAMPLE in request",
            "for [redacted:aws_access_key_id] in request",
            id="aws_access_key_id",
        ),
        pytest.param(
            "postgresql://user:synthetic@db:5432/vector failed",
            "postgresql://[redacted:url_userinfo]@db:5432/vector failed",
            id="url_userinfo",
        ),
        pytest.param(
            "got eyJabc.eyJdef.signature failed",
            "got [redacted:jwt] failed",
            id="jwt",
        ),
        pytest.param(
            "connect failed password=synthetic host=db",
            "connect failed password=[redacted:credential]",
            id="credential_assignment",
        ),
    ],
)
def test_each_known_credential_kind_is_replaced(text: str, expected: str) -> None:
    """入口は列挙した種類の認証情報を、それぞれ種別付きの表記に置き換える。"""
    assert prevent_credential_leaks(text) == expected


def test_multiple_credential_kinds_in_one_text_are_all_replaced() -> None:
    """同じ文に複数の種類が混ざっても、それぞれの位置で置き換える。"""
    text = (
        "for AKIAIOSFODNN7EXAMPLE got eyJabc.eyJdef.signature "
        "from https://user:synthetic@host/path password=synthetic host=db"
    )
    assert prevent_credential_leaks(text) == (
        "for [redacted:aws_access_key_id] got [redacted:jwt] "
        "from https://[redacted:url_userinfo]@host/path "
        "password=[redacted:credential]"
    )


def test_private_key_after_credential_key_leaves_no_fragment() -> None:
    """認証キーの後ろにPEMがあっても、BEGIN行や本文の断片を残さない。"""
    assert prevent_credential_leaks(f"private_key={_PEM_BLOCK} failed") == (
        "private_key=[redacted:credential]"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "Connection refused on host db.internal port 5432", id="connection_error"
        ),
        pytest.param(
            "https://www.anthropic.com/news/claude-4-release", id="public_url"
        ),
        pytest.param("postgres://db:5432/vector", id="url_without_userinfo"),
        pytest.param(
            "arn:aws:ssm:ap-northeast-1:123456789012:parameter/vector/db", id="arn"
        ),
        pytest.param(
            "vector-db.cluster-abc.ap-northeast-1.rds.amazonaws.com:5432",
            id="rds_endpoint",
        ),
        pytest.param("completion_tokens=128 max_tokens=1024", id="token_metrics"),
        pytest.param("取得に失敗しました: timeout after 30s", id="japanese"),
    ],
)
def test_text_without_credentials_is_preserved(text: str) -> None:
    """認証情報を含まない原因文・識別情報・計量値を変えない。"""
    assert prevent_credential_leaks(text) == text


def test_leak_prevention_does_not_truncate_text() -> None:
    """漏洩防止は置き換えだけを担い、文字数の上限を暗黙に適用しない。"""
    text = "y" * (TEXT_LIMIT * 3)
    assert prevent_credential_leaks(text) == text
