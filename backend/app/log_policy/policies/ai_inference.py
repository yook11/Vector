"""AI_INFERENCE のログポリシー。許可項目は未定義。"""

from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS
from app.log_policy.rules import LogPolicy, LogPolicyRules

AI_INFERENCE_POLICY = LogPolicyRules(
    policy=LogPolicy.AI_INFERENCE,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
)
