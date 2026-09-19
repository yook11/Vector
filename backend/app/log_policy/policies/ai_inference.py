"""AI推論の本文禁止と、モデル・トークン数を記録する完成済み規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

AI_INFERENCE_POLICY = LogPolicyRules(
    policy=LogPolicy.AI_INFERENCE,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
    mask=ARTICLE_TEXT_KEYS,
)

AI_INFERENCE_LOG_RULES = AI_INFERENCE_POLICY.extend(
    allow=frozenset({"model", "input_tokens", "output_tokens"}),
)
