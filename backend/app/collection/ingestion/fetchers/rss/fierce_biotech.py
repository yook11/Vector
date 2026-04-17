"""FierceBiotech RSS フェッチャー。"""

from datetime import UTC, datetime

from app.collection.ingestion.fetchers.rss.base import (
    BaseRssFetcher,
    extract_guid,
    parse_published_date,
)
from app.collection.ingestion.persister import ArticleCandidate, to_safe_url
from app.utils.sanitize import strip_html_tags


def _parse_fierce_date(raw: str) -> datetime | None:
    """FierceBiotech 独自の日付フォーマットをパースする。

    形式例: "Apr 17, 2026 12:23pm"
    feedparser は published_parsed を返さない（検証済み）ため、
    生文字列から直接パースする。
    .upper() で正規化する理由: %p の大文字小文字挙動がプラットフォーム依存。
    """
    try:
        return datetime.strptime(raw.upper(), "%b %d, %Y %I:%M%p").replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return None


class FierceBiotechFetcher(BaseRssFetcher):
    """FierceBiotech 用フェッチャー。独自日付フォーマットのフォールバックパース。"""

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        raw_url = entry.get("link", "") or extract_guid(entry) or ""
        if not raw_url:
            return None

        safe_url = to_safe_url(raw_url)
        if safe_url is None:
            return None

        # feedparser の標準パースを試み、失敗時に独自フォーマットでフォールバック
        published_at = parse_published_date(entry)
        if published_at is None:
            raw_date = entry.get("published", "")
            if raw_date:
                published_at = _parse_fierce_date(raw_date)

        return ArticleCandidate(
            url=safe_url,
            title=strip_html_tags(entry.get("title", ""))[:500],
            description=strip_html_tags(
                entry.get("summary") or entry.get("description")
            ),
            published_at=published_at,
        )
