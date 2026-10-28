"""AIプロバイダー呼び出しの資源終了と枠超過の診断、記事分析の相関情報を記録する規則。"""

from app.log_policy.policies.ai_inference import AI_INFERENCE_POLICY

AI_PROVIDER_LOG_RULES = AI_INFERENCE_POLICY.extend(
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
