"""ITmedia RSS フェッチャー。"""

import re

from app.collection.ingestion.fetchers.rss.base import (
    BaseRssFetcher,
    extract_guid,
    parse_published_date,
)
from app.collection.ingestion.persister import ArticleCandidate, to_safe_url
from app.utils.sanitize import strip_html_tags

# [ITmedia PC USER], [ITmedia エンタープライズ] 等の接頭辞を除去する。
# \w+ ではなく [^\]]+ を使用: 空白やマルチバイト文字を含むセクション名に対応。
_TITLE_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


class ITmediaFetcher(BaseRssFetcher):
    """ITmedia 用フェッチャー。タイトル接頭辞を除去する。"""

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        raw_url = entry.get("link", "") or extract_guid(entry) or ""
        if not raw_url:
            return None

        safe_url = to_safe_url(raw_url)
        if safe_url is None:
            return None

        title = strip_html_tags(entry.get("title", ""))[:500]
        title = _TITLE_PREFIX_RE.sub("", title)

        return ArticleCandidate(
            url=safe_url,
            title=title,
            description=strip_html_tags(
                entry.get("summary") or entry.get("description")
            ),
            published_at=parse_published_date(entry),
        )
