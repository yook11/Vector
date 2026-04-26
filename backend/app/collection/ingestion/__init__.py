"""Ingestion — 外部ソースから記事レコードを取り込む。

ユビキタス語彙:

- ``ArticleCandidate``: fetcher 境界の正規化 VO (URL 安全性 / タイトル整形済み)。
- ``DiscoveredArticleDraft``: 永続化前のドメイン入力 VO (candidate +
  ``news_source_id``)。
- ``DiscoveredArticleEntity``: システムに記録された Entity (identity / 発見時刻)。
- ``DiscoveredArticleRepository``: Draft → save_many → Entity / URL → find_by_url。
- ``SourceFetchService``: ソース 1 件のメタデータ取得ユースケース。
- ``SourceFetchOutcome``: Service の戻り値 tagged union (``SourceFetchedOutcome``
  / ``SourceNotFoundOutcome`` / ``QuotaSkippedOutcome``)。

NOTE: ``SourceFetchOutcome`` および各 variant は **内部結果型**。HTTP レスポンス
として直接返却してはならない (URL/title 等の漏出経路を作らない、security R1)。
Task は変換した payload dict を taskiq 戻り値として返す責務を持つ。
"""

from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
    DiscoveredArticleEntity,
)
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.collection.ingestion.service import (
    QuotaSkippedOutcome,
    SourceFetchedOutcome,
    SourceFetchOutcome,
    SourceFetchService,
    SourceNotFoundOutcome,
)

__all__ = [
    "ArticleCandidate",
    "DiscoveredArticleDraft",
    "DiscoveredArticleEntity",
    "DiscoveredArticleRepository",
    "QuotaSkippedOutcome",
    "SourceFetchOutcome",
    "SourceFetchService",
    "SourceFetchedOutcome",
    "SourceNotFoundOutcome",
]
