"""外部の記事を取得する工程の本文禁止と、処理の識別・結果・診断を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

EXTERNAL_CONTENT_POLICY = LogPolicyRules(
    policy=LogPolicy.EXTERNAL_CONTENT_FETCH,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
)

EXTERNAL_CONTENT_LOG_RULES = EXTERNAL_CONTENT_POLICY.extend(
    allow=frozenset(
        {
            "service",
            "environment",
            "stage",
            "request_id",
            "message_id",
            "event_id",
            "incomplete_article_id",
            "source_id",
            "outcome",
            "analyzable_article_id",
            "code",
            "reason",
            "requires_investigation",
            "retry_at",
            "duration_ms",
            "message_disposition",
            "result",
            "visibility_timeout_seconds",
            "capped",
            "operation",
            "resource",
            "error_class",
        }
    ),
)
