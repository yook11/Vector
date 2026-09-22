"""秘密情報取得の資源終了診断と相関情報を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules

SECRET_ACCESS_LOG_RULES = LogPolicyRules(
    policy=LogPolicy.INFRASTRUCTURE,
    allow=frozenset(
        {
            "service",
            "environment",
            "stage",
            "request_id",
            "message_id",
            "event_id",
            "operation",
            "resource",
            "error_class",
        }
    ),
)
