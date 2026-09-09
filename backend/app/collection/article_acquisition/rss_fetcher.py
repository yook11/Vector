"""RSS宣言とソース固有処理から共通の記事材料を取得する。"""

import html
import re
from collections.abc import AsyncIterator
from typing import assert_never

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.sources.rss_acquisition import RssBodyPolicy, RssSource
from app.collection.sources.rss_hooks import (
    RequiresBodyTransform,
    RequiresPublishedAtResolution,
    RequiresScopeFilter,
    RequiresUrlTransform,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _select_body(entry: RssEntry, policy: RssBodyPolicy) -> str | None:
    match policy:
        case RssBodyPolicy.DISCARD:
            return None
        case RssBodyPolicy.CONTENT_ENCODED:
            return entry.content_encoded or ""
        case RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY:
            content = entry.content_encoded or ""
            summary = entry.summary or ""
            return content if len(content) >= len(summary) else summary
        case RssBodyPolicy.CONTENT_OR_SUMMARY:
            return entry.content_encoded or entry.summary or ""
        case _:
            assert_never(policy)


def _plain_body(body: str) -> str:
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", body))).strip()


class RssFetcher:
    def __init__(self, reader: RssReader) -> None:
        self._reader = reader

    async def fetch(self, source: RssSource) -> AsyncIterator[FetchedArticle]:
        acquisition = source.acquisition
        # 複数フィードは部分失敗の契約を実装するスライス3まで拒否する。
        if len(acquisition.feeds) != 1:
            raise ValueError("multiple RSS feeds are not supported yet")
        entries = await self._reader.fetch(
            endpoint_url=acquisition.feeds[0],
            source_name=str(source.name),
            parse_mode=acquisition.parse_mode,
        )
        if isinstance(source, RequiresScopeFilter):
            entries = [entry for entry in entries if source.in_scope(entry)]
        for entry in entries:
            url = (
                source.transform_url(entry.link)
                if isinstance(source, RequiresUrlTransform)
                else entry.link
            )
            published_at = (
                source.resolve_published_at(entry)
                if isinstance(source, RequiresPublishedAtResolution)
                else entry.published
            )
            body = _select_body(entry, acquisition.body_policy)
            if body is not None:
                body = _plain_body(body)
                if isinstance(source, RequiresBodyTransform):
                    body = source.transform_body(body)
            yield FetchedArticle(
                title=entry.title,
                url=url,
                body=body or None,
                published_at=published_at,
            )
