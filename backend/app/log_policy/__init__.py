"""ログポリシー: 基底 deny・目的ポリシー・structlog processor。

契約は specs/observability/logging-base-policy.md を参照。
"""

from app.log_policy.base import (
    BASE_ALLOW,
    BASE_DENY,
    BASE_LOG_RULES,
    BASE_MASK,
    LogPolicy,
    LogPolicyRules,
)
from app.log_policy.chain import build_processors
from app.log_policy.processor import LogPolicyProcessor

__all__ = [
    "BASE_ALLOW",
    "BASE_DENY",
    "BASE_LOG_RULES",
    "BASE_MASK",
    "LogPolicy",
    "LogPolicyProcessor",
    "LogPolicyRules",
    "build_processors",
]
