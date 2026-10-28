"""週次トレンドの要点とメンションの名前を禁止し、週・カテゴリ・件数を記録する規則。"""

from app.log_policy.base import LogPolicy, LogPolicyRules
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS

TREND_DISCOVERY_POLICY = LogPolicyRules(
    policy=LogPolicy.TREND_DISCOVERY,
    allow=frozenset(),
    # 集計の元の要点から取り出したメンションの名前を禁止する。
    deny=ARTICLE_TEXT_KEYS | frozenset({"surface", "display_name", "co_mention_name"}),
)

TREND_DISCOVERY_LOG_RULES = TREND_DISCOVERY_POLICY.extend(
    allow=frozenset(
        {
            "window_end",
            "source_analysis_count",
            "category_count",
            "category_id",
        }
    ),
)
