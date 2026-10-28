"""外部の記事を取得する工程の本文禁止と、処理の識別・結果・診断を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

ARTICLE_FETCH_POLICY = LogPolicyRules(
    policy=LogPolicy.ARTICLE_FETCH,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
)

ARTICLE_FETCH_LOG_RULES = ARTICLE_FETCH_POLICY.extend(
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
            "acquisition_request_id",
            "created_count",
            "source_name",
            "feed_url",
            "entries_count",
            "outcome_code",
            "article_type",
            "has_title",
            "title_origin",
            "has_body",
            "body_origin",
            "body_length",
            "has_published_at",
            "published_at_origin",
            "business_outcome_code",
        }
    ),
)
