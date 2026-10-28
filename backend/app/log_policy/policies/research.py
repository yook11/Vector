"""リサーチの利用者の文と記事本文を禁止し、Runの識別・状態・件数を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

RESEARCH_POLICY = LogPolicyRules(
    policy=LogPolicy.RESEARCH,
    allow=frozenset(),
    # 利用者の質問と、そこから作る回答・検索語を禁止する。
    deny=ARTICLE_TEXT_KEYS
    | frozenset({"question", "previous_answer", "query", "queries"}),
)

RESEARCH_LOG_RULES = RESEARCH_POLICY.extend(
    allow=frozenset(
        {
            "run_id",
            "attempt_epoch",
            "generation",
            "observed_status",
            "terminal_status",
            "reason",
            "cause",
            "failure_code",
            "operation",
            "event_type",
            "usage_date",
            "used_count",
            "limit",
            "queued_count",
            "running_count",
            "quota_released_count",
            "quota_not_eligible_count",
            "quota_inconsistent_count",
            "count",
            "run_count",
            "query_count",
            "marker_without_source_refs",
            "source_without_marker_refs",
            "model",
        }
    ),
)
