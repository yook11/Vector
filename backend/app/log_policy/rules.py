"""ログポリシーの規則定義。基底 deny と、目的ポリシーがそれを継承する型。

基底は「どの目的の処理からも出してはいけないキー」だけを持ち、allow を持たない。
目的ポリシーは基底 deny を和集合で継承し、自分の allow / deny を足すことしか
できない (引く操作は存在しない)。IAM と同じく explicit deny は allow に常に優先する。
契約は specs/observability/logging-base-policy.md を参照。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class LogPolicy(StrEnum):
    """logger 構築時に宣言する、適用するポリシーの識別子。"""

    EXTERNAL_CONTENT_FETCH = "external_content_fetch"
    AI_INFERENCE = "ai_inference"
    USER_INTERACTION = "user_interaction"
    PIPELINE_CONTROL = "pipeline_control"
    INFRASTRUCTURE = "infrastructure"


# 知っていれば他人になりすませる値のキー。識別情報 (host / ARN 等) は含めない。
CREDENTIAL_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
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
        "x_api_key",
        "x_goog_api_key",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "pgpassword",
        "private_key",
        "client_secret",
        "access_token",
        "refresh_token",
        "id_token",
        "access_key_id",
        "secret_access_key",
        "session_token",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
    }
)

BASE_DENY = CREDENTIAL_KEYS

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")


def normalize_key(key: str) -> str:
    """deny / allow 照合用にキー名を snake_case 小文字へ正規化する。"""
    key = _ACRONYM_BOUNDARY.sub(r"\1_\2", key)
    return _CAMEL_BOUNDARY.sub(r"_\1", key).replace("-", "_").lower()


@dataclass(frozen=True, slots=True)
class LogPolicyRules:
    """目的ポリシー 1 つ分の規則。基底 deny を継承し、足すことしかできない。"""

    policy: LogPolicy
    allow: frozenset[str]
    deny: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # allow と有効 deny の重複は定義時に落とし、processor に到達させない。
        overlap = self.normalized_allow & self.effective_deny
        if overlap:
            raise ValueError(
                f"{self.policy.value}: allow が deny と重複: {sorted(overlap)}"
            )

    @property
    def effective_deny(self) -> frozenset[str]:
        return BASE_DENY | frozenset(normalize_key(k) for k in self.deny)

    @property
    def normalized_allow(self) -> frozenset[str]:
        return frozenset(normalize_key(k) for k in self.allow)
