"""基底allow・denyを、生成時に確定するログ規則。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class LogPolicy(StrEnum):
    """logger 構築時に宣言する、適用するポリシーの識別子。"""

    ARTICLE_FETCH = "article_fetch"
    ARTICLE_ANALYSIS = "article_analysis"
    AI_PROVIDER = "ai_provider"
    CACHE_REVALIDATION = "cache_revalidation"
    RESEARCH = "research"
    BRIEFING = "briefing"
    TREND_DISCOVERY = "trend_discovery"
    PIPELINE_CONTROL = "pipeline_control"
    PROCESS_RUNTIME = "process_runtime"
    SECRET_ACCESS = "secret_access"  # noqa: S105 — 秘密値ではなくログ方針の識別子。
    AUTH_RATE_LIMIT_CLEANUP = "auth_rate_limit_cleanup"


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

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")


def normalize_key(key: str) -> str:
    """ポリシー照合用に項目名を snake_case 小文字へ正規化する。"""
    key = _ACRONYM_BOUNDARY.sub(r"\1_\2", key)
    return _CAMEL_BOUNDARY.sub(r"_\1", key).replace("-", "_").lower()


@dataclass(frozen=True, slots=True)
class LogPolicyRules:
    """基底を含む完成済み規則を保持し、継承時も親のdenyを維持する。"""

    policy: LogPolicy | None
    allow: frozenset[str]
    deny: frozenset[str] = field(default_factory=frozenset)

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

    def extend(
        self,
        *,
        allow: frozenset[str],
        deny: frozenset[str] = frozenset(),
    ) -> LogPolicyRules:
        """親のdenyを維持し、明示した許可項目で新しい規則を定義する。"""
        return LogPolicyRules(
            policy=self.policy,
            allow=allow,
            deny=self.deny | deny,
        )


BASE_LOG_RULES = LogPolicyRules(policy=None, allow=frozenset())
