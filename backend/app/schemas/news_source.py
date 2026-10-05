"""ニュースソースの API 表現。"""

from app.collection.sources.source_name import SourceName
from app.schemas.base import _CamelBase


class NewsSource(_CamelBase):
    """読者に見せるニュースソース。

    管理用の項目 (取得 URL・有効/無効など) は持たない。
    """

    name: SourceName
    attribution_label: str | None = None
