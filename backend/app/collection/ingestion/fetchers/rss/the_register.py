"""The Register RSS フェッチャー。"""

from app.collection.ingestion.domain import ArticleCandidate
from app.collection.ingestion.fetchers.rss.base import BaseRssFetcher, extract_guid

# The Register の Atom フィードはリダイレクタ経由のリンクを返す。
# 例: https://go.theregister.com/feed/www.theregister.com/2026/04/28/<slug>/
#       → https://www.theregister.com/2026/04/28/<slug>/
# プレフィックス直後にホストを含む実 URL のパスが続くため、
# 接頭辞を切り捨てて再構築する。
_REDIRECTOR_PREFIX = "https://go.theregister.com/feed/"


class TheRegisterFetcher(BaseRssFetcher):
    """The Register 用フェッチャー。リダイレクタ URL を実 URL に正規化する。"""

    def convert_entry(self, entry: dict) -> ArticleCandidate | None:
        raw_link = entry.get("link", "") or extract_guid(entry) or ""
        if raw_link.startswith(_REDIRECTOR_PREFIX):
            raw_url = "https://" + raw_link[len(_REDIRECTOR_PREFIX) :]
        else:
            raw_url = raw_link
        return ArticleCandidate.from_external(
            raw_url=raw_url, raw_title=entry.get("title", "")
        )
