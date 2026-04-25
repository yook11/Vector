"""ingestion BC のドメイン層。

外部ソース (RSS / HN API 等) から発見された記事の概念を表現する:

- ``ArticleCandidate`` — fetcher 境界 (技術境界) の正規化 VO。
  外部生文字列 → URL 安全性 / タイトル整形済みの中間表現。
- ``DiscoveredArticleDraft`` — 永続化前のドメイン入力 VO。candidate を
  ``news_source_id`` と束ねた状態。
- ``DiscoveredArticleEntity`` — システムに記録された Entity。identity (id) と
  発見時刻 (discovered_at) を持ち、下流 BC が id で参照する。
"""

from app.collection.ingestion.domain.discovered_article import (
    DiscoveredArticleDraft,
    DiscoveredArticleEntity,
)
from app.collection.ingestion.domain.value_objects import ArticleCandidate

__all__ = [
    "ArticleCandidate",
    "DiscoveredArticleDraft",
    "DiscoveredArticleEntity",
]
