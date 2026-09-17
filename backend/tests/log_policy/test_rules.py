"""``LogPolicyRules`` の定義時契約: allow と deny の排他。"""

from __future__ import annotations

import pytest

from app.log_policy import LogPolicy, LogPolicyRules

pytestmark = pytest.mark.unit


def test_allow_containing_base_deny_key_fails_at_definition() -> None:
    """allow に基底の禁止キーを含めると、定義の時点で失敗する。"""
    with pytest.raises(ValueError, match="allow が deny と重複"):
        LogPolicyRules(
            policy=LogPolicy.AI_INFERENCE,
            allow=frozenset({"model", "prompt", "api_key"}),
        )


def test_allow_containing_own_deny_key_fails_at_definition() -> None:
    """目的ポリシー自身の deny と allow の重複も定義時に落ちる。"""
    with pytest.raises(ValueError, match="allow が deny と重複"):
        LogPolicyRules(
            policy=LogPolicy.USER_INTERACTION,
            allow=frozenset({"run_id", "question"}),
            deny=frozenset({"question"}),
        )


@pytest.mark.parametrize(
    "key",
    [
        "aws_secret_access_key",
        "session_token",
        "AWSAccessKeyId",
        "APIKey",
        "privateKey",
        "Proxy-Authorization",
        "Set-Cookie",
        "refresh_token",
    ],
)
def test_credential_alias_cannot_be_allowed(key: str) -> None:
    """認証情報の表記揺れも正規化後は基底 deny と重なり、定義時に落ちる。"""
    with pytest.raises(ValueError, match="allow が deny と重複"):
        LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({key}))
