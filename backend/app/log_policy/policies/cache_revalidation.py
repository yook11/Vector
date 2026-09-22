"""キャッシュ更新通知の対象・処理箇所・相関情報を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules

CACHE_REVALIDATION_LOG_RULES = LogPolicyRules(
    policy=LogPolicy.CACHE_REVALIDATION,
    allow=frozenset(
        {
            "service",
            "environment",
            "stage",
            "request_id",
            "message_id",
            "event_id",
            "tags",
            "operation",
            "error_class",
        }
    ),
)
