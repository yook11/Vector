"""AI推論の本文禁止と、処理の識別・結果・使用量を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

AI_INFERENCE_POLICY = LogPolicyRules(
    policy=LogPolicy.AI_INFERENCE,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
    mask=ARTICLE_TEXT_KEYS,
)

AI_INFERENCE_LOG_RULES = AI_INFERENCE_POLICY.extend(
    allow=frozenset(
        {
            "service",
            "environment",
            "stage",
            "operation",
            "request_id",
            "message_id",
            "event_id",
            "curation_id",
            "analyzable_article_id",
            "analyzed_article_id",
            "outcome",
            "rejection_code",
            "duration_ms",
            "message_disposition",
            "model",
            "input_tokens",
            "output_tokens",
        }
    ),
)
