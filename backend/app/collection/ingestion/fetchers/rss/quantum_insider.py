"""The Quantum Insider RSS フェッチャー。"""

from app.collection.ingestion.fetchers.rss.base import (
    BaseRssFetcher,
    extract_full_content,
    extract_guid,
    parse_published_date,
)
from app.collection.ingestion.persister import ArticleCandidate, to_safe_url
from app.utils.sanitize import strip_html_tags


class QuantumInsiderFetcher(BaseRssFetcher):
    """The Quantum Insider 用フェッチャー。content:encoded の全文を取得する。

    content:encoded があれば original_content に格納する。
    これにより fetch_content タスクがスキップされ、
    直接 analyze_article にチェーンされる（tasks.py の分岐条件による）。
    """

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        raw_url = entry.get("link", "") or extract_guid(entry) or ""
        if not raw_url:
            return None

        safe_url = to_safe_url(raw_url)
        if safe_url is None:
            return None

        return ArticleCandidate(
            url=safe_url,
            title=strip_html_tags(entry.get("title", ""))[:500],
            description=strip_html_tags(
                entry.get("summary") or entry.get("description")
            ),
            content=extract_full_content(entry),
            published_at=parse_published_date(entry),
        )
