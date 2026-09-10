"""複数フィードの巡回・失敗隔離・候補選択を共通取得入口から検証する。"""

from dataclasses import replace
from unittest.mock import AsyncMock, call

import pytest
from structlog.testing import capture_logs

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.sources.definitions.cornell import CornellChronicleSource
from app.collection.sources.definitions.nasa import NASASource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.rss_acquisition import RssSource

_FEEDS = ("https://example.test/a", "https://example.test/b", "https://example.test/c")
_ENTRY = RssEntry(
    link="https://example.test/article",
    title="first",
    guid=None,
    published=None,
    summary="summary",
    content_encoded="<p>first body</p>",
    tags=(),
    raw_published=None,
    raw_updated=None,
)


class Source(TechCrunchSource):
    acquisition = replace(TechCrunchSource.acquisition, feeds=_FEEDS)


async def _collect(
    reader: RssReader, source: RssSource = Source
) -> list[FetchedArticle]:
    return [item async for item in fetch_articles(source, ReaderTools(rss=reader))]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "bytes"])
async def test_traversal_preserves_feed_and_entry_order_without_implicit_dedup(
    mode: str,
) -> None:
    class OrderedSource(Source):
        acquisition = replace(Source.acquisition, parse_mode=mode)

    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = [[_ENTRY, replace(_ENTRY, title="second")], [], [_ENTRY]]
    with capture_logs() as logs:
        result = await _collect(reader, OrderedSource)
    assert [a.title for a in result] == ["first", "second", "first"]
    assert reader.fetch.await_args_list == [
        call(endpoint_url=url, source_name=str(Source.name), parse_mode=mode)
        for url in _FEEDS
    ]
    success = [log for log in logs if log["event"] == "source_feed_fetched"]
    assert [(log["feed"], log["entries_count"]) for log in success] == list(
        zip(_FEEDS, [2, 0, 1], strict=True)
    )
    assert all(log["source"] == str(Source.name) for log in success)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        FetchOriginServerError(status_code=503, reason="unavailable"),
        FetchResourceNotFoundError(status_code=404, reason="not_found"),
        UnreadableResponseError(
            reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
        ),
    ],
)
async def test_partial_failure_is_logged_and_other_feeds_continue(
    error: ExternalFetchError | UnreadableResponseError,
) -> None:
    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = [[_ENTRY], error, [_ENTRY]]
    with capture_logs() as logs:
        result = await _collect(reader)
    assert len(result) == 2
    assert reader.fetch.await_count == 3
    failed = [log for log in logs if log["event"] == "source_feed_fetch_failed"]
    assert failed == [
        dict(
            event="source_feed_fetch_failed",
            source=str(Source.name),
            feed=_FEEDS[1],
            code=error.CODE,
            error=str(error),
            log_level="warning",
        )
    ]


@pytest.mark.asyncio
async def test_all_failures_preserve_first_exception_and_cause() -> None:
    reader = AsyncMock(spec=RssReader)
    cause = ValueError("parser cause")
    first = UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
    )
    first.__cause__ = cause
    reader.fetch.side_effect = [
        first,
        FetchResourceNotFoundError(status_code=404, reason="not_found"),
        FetchOriginServerError(status_code=503, reason="last_failure"),
    ]
    with capture_logs() as logs, pytest.raises(UnreadableResponseError) as caught:
        await _collect(reader)
    assert caught.value is first
    assert caught.value.__cause__ is cause
    assert reader.fetch.await_count == 3
    assert len([log for log in logs if log["event"] == "source_feed_fetch_failed"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_only", [True, False])
async def test_successful_empty_feed_counts_as_success(empty_only: bool) -> None:
    reader = AsyncMock(spec=RssReader)
    error = FetchOriginServerError(status_code=503, reason="unavailable")
    reader.fetch.side_effect = [[], [], []] if empty_only else [error, [], error]
    assert await _collect(reader) == []
    assert reader.fetch.await_count == 3


@pytest.mark.asyncio
async def test_unexpected_reader_failure_stops_before_later_feeds_and_selection() -> (
    None
):
    class MustNotSelect(Source):
        @staticmethod
        def select(entries: list[RssEntry]) -> list[RssEntry]:
            pytest.fail("取得が完了しなければ候補を選択しない")

    reader = AsyncMock(spec=RssReader)
    error = RuntimeError("reader bug")
    reader.fetch.side_effect = [[_ENTRY], error, [_ENTRY]]
    with capture_logs() as logs, pytest.raises(RuntimeError) as caught:
        await _collect(reader, MustNotSelect)
    assert caught.value is error
    assert reader.fetch.await_count == 2
    assert not [log for log in logs if log["event"] == "source_feed_fetch_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [True, False])
async def test_selection_runs_once_after_scope_and_controls_mapping_order(
    empty: bool,
) -> None:
    trace: list[str] = []

    class SelectingSource(Source):
        @staticmethod
        def in_scope(entry: RssEntry) -> bool:
            trace.append(f"scope:{entry.title}")
            return entry.title != "drop"

        @staticmethod
        def select(entries: list[RssEntry]) -> list[RssEntry]:
            trace.append("select")
            assert [entry.title for entry in entries] == (
                [] if empty else ["first", "last"]
            )
            return entries[::-1]

        @staticmethod
        def transform_url(url: str) -> str:
            trace.append(f"url:{url}")
            return url

    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = (
        [[], [], []]
        if empty
        else [
            [_ENTRY],
            [replace(_ENTRY, title="drop")],
            [replace(_ENTRY, title="last", link="last")],
        ]
    )
    result = await _collect(reader, SelectingSource)
    if empty:
        assert trace == ["select"]
        assert result == []
    else:
        assert trace == [
            "scope:first",
            "scope:drop",
            "scope:last",
            "select",
            "url:last",
            f"url:{_ENTRY.link}",
        ]
        assert [a.title for a in result] == ["last", "first"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("selection bug"),
        FetchResourceNotFoundError(status_code=404, reason="not_found"),
    ],
)
async def test_selection_exception_is_not_caught_as_feed_failure(
    error: Exception,
) -> None:
    cause = ValueError("cause")

    class FailingSource(Source):
        @staticmethod
        def select(entries: list[RssEntry]) -> list[RssEntry]:
            raise error from cause

    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = [_ENTRY]
    with capture_logs() as logs, pytest.raises(type(error)) as caught:
        await _collect(reader, FailingSource)
    assert caught.value is error
    assert caught.value.__cause__ is cause
    assert reader.fetch.await_count == 3
    assert not [log for log in logs if log["event"] == "source_feed_fetch_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [NASASource, CornellChronicleSource])
async def test_sources_keep_first_content_and_all_empty_links_without_mutation(
    source: RssSource,
) -> None:
    first = [_ENTRY, replace(_ENTRY, link="")]
    second = [
        replace(_ENTRY, title="later", content_encoded="richer body"),
        replace(_ENTRY, link=""),
    ]
    original = first.copy(), second.copy()
    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = [first, second, [], [], [], []]
    result = await _collect(reader, source)
    assert [a.url for a in result] == [_ENTRY.link, "", ""]
    assert result[0].title == "first"
    assert result[0].body == ("first body" if source is NASASource else None)
    assert (first, second) == original
    assert [c.kwargs["endpoint_url"] for c in reader.fetch.await_args_list] == list(
        source.acquisition.feeds
    )
    assert all(
        c.kwargs["parse_mode"] == ("text" if source is NASASource else "bytes")
        for c in reader.fetch.await_args_list
    )
