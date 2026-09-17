"""EXTERNAL_CONTENT_FETCH のログポリシー。許可項目は未定義。"""

from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS
from app.log_policy.rules import LogPolicy, LogPolicyRules

EXTERNAL_CONTENT_POLICY = LogPolicyRules(
    policy=LogPolicy.EXTERNAL_CONTENT_FETCH,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
)
