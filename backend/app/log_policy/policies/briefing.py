"""週次ブリーフィングの記事本文とAIが書いた文を禁止し、週・カテゴリ・件数を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

BRIEFING_POLICY = LogPolicyRules(
    policy=LogPolicy.BRIEFING,
    allow=frozenset(),
    # AIへ渡す記事本文と、AIが書いた見出し・章を禁止する。
    deny=ARTICLE_TEXT_KEYS | frozenset({"prompt", "headline", "chapters"}),
)

BRIEFING_LOG_RULES = BRIEFING_POLICY.extend(
    allow=frozenset(
        {
            "week_start",
            "category_id",
            "category_slug",
            "category_name",
            "article_count",
            "selected_category_count",
            "enqueued_category_count",
            "failed_category_count",
            "model",
        }
    ),
)
