"""EXTERNAL_CONTENT_FETCH のログポリシー。許可項目は未定義。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

EXTERNAL_CONTENT_POLICY = LogPolicyRules(
    policy=LogPolicy.EXTERNAL_CONTENT_FETCH,
    allow=frozenset(),
    deny=ARTICLE_TEXT_KEYS,
)
