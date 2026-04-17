"""Cointelegraph RSS フェッチャー。"""

from urllib.parse import parse_qs, urlparse, urlunparse

from app.collection.ingestion.fetchers.rss.base import (
    BaseRssFetcher,
    extract_guid,
    parse_published_date,
)
from app.collection.ingestion.persister import ArticleCandidate, to_safe_url
from app.utils.sanitize import strip_html_tags


def _strip_utm_params(url: str) -> str:
    """URL から UTM パラメータを除去する。"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in params.items() if not k.startswith("utm_")}

    if not cleaned:
        clean_url = urlunparse(parsed._replace(query=""))
    else:
        from urllib.parse import urlencode

        clean_url = urlunparse(parsed._replace(query=urlencode(cleaned, doseq=True)))
    return clean_url


class CointelegraphFetcher(BaseRssFetcher):
    """Cointelegraph 用フェッチャー。UTM パラメータを除去する。"""

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        raw_url = entry.get("link", "") or extract_guid(entry) or ""
        if not raw_url:
            return None

        # UTM 除去は to_safe_url の前に適用（SafeUrl は URL を正規化しない）
        clean_url = _strip_utm_params(raw_url)
        safe_url = to_safe_url(clean_url)
        if safe_url is None:
            return None

        return ArticleCandidate(
            url=safe_url,
            title=strip_html_tags(entry.get("title", ""))[:500],
            description=strip_html_tags(
                entry.get("summary") or entry.get("description")
            ),
            published_at=parse_published_date(entry),
        )
