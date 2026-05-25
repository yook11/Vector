"""``FetchCadence`` — source ごとの取得間隔 tier (意味分類)。

各 ``ArticleSource`` が ClassVar として宣言する。tier → cron の写像は dispatch
側 (``app.brokers.CADENCE_CRON``) が持ち、本 enum は「どの頻度帯に属すか」の
意味分類だけを担う leaf module (循環依存を持たない)。
"""

from enum import StrEnum


class FetchCadence(StrEnum):
    """取得間隔 tier。実 cron は ``app.brokers.CADENCE_CRON`` で写像する。"""

    HIGH = "high"  # 短間隔 (商業テックメディア等)
    MEDIUM = "medium"  # 中間隔 (企業ブログ・専門誌・宇宙・セキュリティ等)
    LOW = "low"  # 長間隔 (政府・学術誌等)
