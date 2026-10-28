"""プロセスの起動・終了・接続の準備と、稼働確認の失敗を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules

PROCESS_RUNTIME_LOG_RULES = LogPolicyRules(
    policy=LogPolicy.PROCESS_RUNTIME,
    allow=frozenset(
        {
            "broker",
            "service",
            "pool_size",
            "max_overflow",
            "capacity",
            "pool_recycle",
            "pool_timeout",
            "generator",
            "model",
        }
    ),
)
