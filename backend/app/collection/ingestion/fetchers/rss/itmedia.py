"""ITmedia RSS フェッチャー。"""

import re

from app.collection.ingestion.candidate import ArticleCandidate
from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher, extract_guid

# [ITmedia PC USER], [ITmedia エンタープライズ] 等の接頭辞を除去する。
# \w+ ではなく [^\]]+ を使用: 空白やマルチバイト文字を含むセクション名に対応。
_TITLE_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


class ITmediaFetcher(BaseRssFetcher):
    """ITmedia 用フェッチャー。タイトル接頭辞を除去する。"""

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        raw_url = entry.get("link", "") or extract_guid(entry) or ""
        raw_title = _TITLE_PREFIX_RE.sub("", entry.get("title", ""))
        return ArticleCandidate.from_external(raw_url=raw_url, raw_title=raw_title)
