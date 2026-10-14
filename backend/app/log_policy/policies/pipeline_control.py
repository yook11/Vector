"""工程を起動する投入の対象・送信結果・相関情報を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules

PIPELINE_CONTROL_LOG_RULES = LogPolicyRules(
    policy=LogPolicy.PIPELINE_CONTROL,
    allow=frozenset(
        {
            "service",
            "environment",
            "stage",
            "request_id",
            "cadence",
            "scheduled_at",
            "operation",
            "requires_investigation",
            "duration_ms",
            "sent_count",
            "acquisition_request_id",
            "source_id",
            "attempts",
            "source_name",
            "raw_source_name",
            "resource",
        }
    ),
)
