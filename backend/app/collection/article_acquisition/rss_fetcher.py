"""RSS宣言とソース固有処理から共通の記事材料を取得する。"""

import html
import re
from collections.abc import AsyncIterator
from typing import assert_never

import structlog

from app.collection.article_acquisition.errors import RssFeedErrors, RssFeedFailure
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
)
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.external_fetch_errors import ExternalFetchError
from app.collection.sources.rss_acquisition import RssBodyPolicy, RssSource
from app.collection.sources.rss_hooks import (
    RequiresBodyTransform,
    RequiresPublishedAtResolution,
    RequiresScopeFilter,
    RequiresSelection,
    RequiresUrlTransform,
)

logger = structlog.get_logger(__name__)

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
        entries: list[RssEntry] = []
        failures: list[RssFeedFailure] = []
        for feed_url in acquisition.feeds:
            try:
                fetched = await self._reader.fetch(
                    endpoint_url=feed_url,
                    source_name=str(source.name),
                    parse_mode=acquisition.parse_mode,
                )
            except (ExternalFetchError, UnreadableResponseError) as exc:
                logger.warning(
                    "source_feed_fetch_failed",
                    source=str(source.name),
                    feed=feed_url,
                    code=exc.CODE,
                    error=str(exc),
                )
                failures.append(RssFeedFailure(feed_url=feed_url, error=exc))
                continue
            logger.info(
                "source_feed_fetched",
                source=str(source.name),
                feed=feed_url,
                entries_count=len(fetched),
            )
            entries.extend(fetched)
        if len(failures) == len(acquisition.feeds):
            raise RssFeedErrors(failures)
        if isinstance(source, RequiresScopeFilter):
            entries = [entry for entry in entries if source.in_scope(entry)]
        if isinstance(source, RequiresSelection):
            entries = source.select(entries)
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
