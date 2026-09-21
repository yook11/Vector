"""``FetchCadence`` — source ごとの取得間隔 tier (意味分類)。

各 ``ArticleSource`` が ClassVar として宣言する。tier → cron の写像は EventBridge
Scheduler 側 (``infra/aws/source_dispatch.tf``) が持ち、本 enum は「どの頻度帯に
属すか」の意味分類だけを担う leaf module (循環依存を持たない)。
"""

from enum import StrEnum


class FetchCadence(StrEnum):
    """取得間隔 tier。実 cron は ``infra/aws/source_dispatch.tf`` が持つ。"""

    HIGH = "high"  # 短間隔 (商業テックメディア等)
    MEDIUM = "medium"  # 中間隔 (企業ブログ・専門誌・宇宙・セキュリティ等)
    LOW = "low"  # 長間隔 (政府・学術誌等)
