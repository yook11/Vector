"""基底allow・deny・maskと項目別sanitizeを、生成時に確定するログ規則。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class LogPolicy(StrEnum):
    """logger 構築時に宣言する、適用するポリシーの識別子。"""

    EXTERNAL_CONTENT_FETCH = "external_content_fetch"
    AI_INFERENCE = "ai_inference"
    CACHE_REVALIDATION = "cache_revalidation"
    USER_INTERACTION = "user_interaction"
    PIPELINE_CONTROL = "pipeline_control"
    INFRASTRUCTURE = "infrastructure"


# パスワード・トークン・APIキーなど、値そのもので認証できる項目のキー名。
# host / ARN などの識別情報は含めない。
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

BASE_ALLOW = frozenset({"event", "level", "timestamp", "logger", "logger_name"})
BASE_DENY = CREDENTIAL_KEYS
BASE_MASK = CREDENTIAL_KEYS

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")


def normalize_key(key: str) -> str:
    """ポリシー照合用に項目名を snake_case 小文字へ正規化する。"""
    key = _ACRONYM_BOUNDARY.sub(r"\1_\2", key)
    return _CAMEL_BOUNDARY.sub(r"_\1", key).replace("-", "_").lower()


@dataclass(frozen=True, slots=True)
class LogPolicyRules:
    """基底を含む完成済み規則を保持し、継承時も親のdeny・mask・sanitizeを維持する。"""

    policy: LogPolicy | None
    allow: frozenset[str]
    deny: frozenset[str] = field(default_factory=frozenset)
    mask: frozenset[str] = field(default_factory=frozenset)
    sanitize: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allow",
            BASE_ALLOW | frozenset(normalize_key(key) for key in self.allow),
        )
        object.__setattr__(
            self,
            "deny",
            BASE_DENY | frozenset(normalize_key(key) for key in self.deny),
        )
        object.__setattr__(
            self,
            "mask",
            BASE_MASK | frozenset(normalize_key(key) for key in self.mask),
        )
        object.__setattr__(
            self,
            "sanitize",
            frozenset(normalize_key(key) for key in self.sanitize),
        )
        # allow と確定済み deny の重複は定義時に落とし、processor に到達させない。
        overlap = self.allow & self.deny
        if overlap:
            name = self.policy.value if self.policy is not None else "base"
            raise ValueError(f"{name}: allow が deny と重複: {sorted(overlap)}")

    def extend(
        self,
        *,
        allow: frozenset[str],
        deny: frozenset[str] = frozenset(),
        mask: frozenset[str] = frozenset(),
        sanitize: frozenset[str] = frozenset(),
    ) -> LogPolicyRules:
        """親のdeny・mask・sanitizeを維持し、明示した許可項目で新しい規則を定義する。"""
        return LogPolicyRules(
            policy=self.policy,
            allow=allow,
            deny=self.deny | deny,
            mask=self.mask | mask,
            sanitize=self.sanitize | sanitize,
        )


BASE_LOG_RULES = LogPolicyRules(policy=None, allow=frozenset())
