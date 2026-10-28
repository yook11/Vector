"""AIプロバイダー呼び出しの資源終了と枠超過の診断、記事分析の相関情報を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

AI_PROVIDER_POLICY = LogPolicyRules(
    policy=LogPolicy.AI_PROVIDER,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
)

AI_PROVIDER_LOG_RULES = AI_PROVIDER_POLICY.extend(
    allow=frozenset(
        {
            "service",
            "environment",
            "stage",
            "request_id",
            "message_id",
            "event_id",
            "analyzable_article_id",
            "curation_id",
            "analyzed_article_id",
            "resource",
            "reason",
            "retry_after_header",
        }
    ),
)
