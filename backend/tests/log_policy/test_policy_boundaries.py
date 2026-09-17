"""アプリ固有の認証情報の共通禁止と、追加 deny の適用を検証する。"""

import json

import pytest

from app.log_policy import LogPolicy, LogPolicyRules
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "key",
    [
        "gemini_api_key",
        "openai_api_key",
        "deepseek_api_key",
        "tavily_api_key",
        "logfire_token",
        "bff_jwt_signing_secret",
        "revalidate_bearer_secret",
        "postgres_auth_password",
        "postgres_app_password",
        "postgres_collect_password",
    ],
)
def test_application_credential_cannot_be_allowed(key) -> None:
    """実際の認証情報名を共通 deny から allow で解除できない。"""
    with pytest.raises(ValueError, match="allow が deny と重複"):
        LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({key}))


@pytest.mark.parametrize("key", ["GEMINI_API_KEY", "openaiApiKey", "Deepseek-Api-Key"])
def test_application_credential_alias_is_denied(key) -> None:
    """設定名の大文字・camelCase・ハイフン表記も禁止する。"""
    output = LogPolicyProcessor()(None, "info", {"event": "failed", key: "synthetic"})
    assert output["_denied_keys"] == [key]
    assert "synthetic" not in json.dumps(output)


def test_custom_purpose_deny_applies_to_text() -> None:
    """テストで指定した追加 deny を文字列にも適用する。"""
    rules = LogPolicyRules(
        LogPolicy.USER_INTERACTION, frozenset(), frozenset({"restricted_sample"})
    )
    output = LogPolicyProcessor([rules])(
        None,
        "info",
        {
            "event": "restricted_sample='synthetic input'",
            "_log_policy": LogPolicy.USER_INTERACTION,
        },
    )
    assert output["event"] == "restricted_sample=***"


def test_custom_deny_applies_to_exception_message() -> None:
    """テストで指定した禁止項目を例外文の変換にも渡す。"""
    rules = LogPolicyRules(
        LogPolicy.INFRASTRUCTURE, frozenset(), frozenset({"restricted_sample"})
    )
    output = LogPolicyProcessor([rules])(
        None,
        "error",
        {
            "event": "failed",
            "_log_policy": LogPolicy.INFRASTRUCTURE,
            "exc_info": ValueError("restricted_sample='synthetic input'"),
        },
    )
    assert output["error_message"] == "restricted_sample=***"
