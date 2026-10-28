"""認証カウンター掃除の削除結果・失敗した処理・相関情報を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules

AUTH_RATE_LIMIT_CLEANUP_LOG_RULES = LogPolicyRules(
    policy=LogPolicy.AUTH_RATE_LIMIT_CLEANUP,
    allow=frozenset(
        {
            "service",
            "environment",
            "request_id",
            "operation",
            "requires_investigation",
            "duration_ms",
            "deleted",
            "cutoff_ms",
        }
    ),
)
