"""複数フィードの巡回・失敗隔離・候補選択を共通取得入口から検証する。"""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, call

import pytest

from app.collection.article_acquisition.errors import (
    ResponseSizeLimitExceededError,
    RssFeedErrors,
)
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.response_size import ResponseSizeBasis
from app.collection.sources.definitions.cornell import CornellChronicleSource
from app.collection.sources.definitions.nasa import NASASource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.rss_acquisition import RssSource
from app.http.destination_policy import (
    DestinationRejectedError,
    DestinationRejectedReason,
)
from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)

_FEEDS = ("https://example.test/a", "https://example.test/b", "https://example.test/c")
_RECEIVED = datetime(2026, 9, 26, tzinfo=UTC)
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


@pytest.fixture
def read_logs(capsys) -> Callable[[], list[dict[str, object]]]:
    """出力されたログを、実行ごとに変わる時刻と発生位置を除いて返す。"""

    def _read() -> list[dict[str, object]]:
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        for log in logs:
            del log["timestamp"]
            log.pop("frames", None)
        return logs

    return _read


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "bytes"])
async def test_traversal_preserves_feed_and_entry_order_without_implicit_dedup(
    mode: str, read_logs
) -> None:
    class OrderedSource(Source):
        acquisition = replace(Source.acquisition, parse_mode=mode)

    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = [[_ENTRY, replace(_ENTRY, title="second")], [], [_ENTRY]]
    result = await _collect(reader, OrderedSource)
    assert [a.title for a in result] == ["first", "second", "first"]
    assert reader.fetch.await_args_list == [
        call(endpoint_url=url, source_name=str(Source.name), parse_mode=mode)
        for url in _FEEDS
    ]
    success = [log for log in read_logs() if log["event"] == "source_feed_fetched"]
    assert [(log["feed_url"], log["entries_count"]) for log in success] == list(
        zip(_FEEDS, [2, 0, 1], strict=True)
    )
    assert all(log["source_name"] == str(Source.name) for log in success)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "exception_fields"),
    [
        (
            HttpResponseError(status_code=503, received_at=_RECEIVED),
            "http_response_error",
            {
                "error_class": "app.http.errors.HttpResponseError",
                "error_message": "HTTPの非成功応答を受け取りました",
                "error_details": {"status_code": 503},
            },
        ),
        (
            HttpTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                )
            ),
            "http_transport_error",
            {
                "error_class": "app.http.errors.HttpTransportError",
                "error_message": "HTTP通信で応答を受け取れませんでした",
                "error_details": {
                    "stage": "receive",
                    "reason": "timeout",
                    "proxy_status": None,
                },
            },
        ),
        (
            DestinationRejectedError(
                reason=DestinationRejectedReason.NON_PUBLIC_IP_LITERAL,
                host="10.0.0.1",
                address="10.0.0.1",
            ),
            "host_blocked",
            {
                "error_class": "app.http.destination_policy.DestinationRejectedError",
                "error_message": "宛先が外部通信の方針で拒否されました",
                "error_details": {"reason": "non_public_ip_literal"},
            },
        ),
        (
            UnreadableResponseError(
                reason=UnreadableResponseReason.MALFORMED_CONTENT,
                response_format="feed",
            ),
            "read_malformed_content",
            {
                "error_class": (
                    "app.collection.article_acquisition.reader.read_errors."
                    "UnreadableResponseError"
                ),
                "error_message": "read_malformed_content: feed",
            },
        ),
        (
            ResponseSizeLimitExceededError(
                limit_bytes=10 * 1024 * 1024,
                observed_bytes=10 * 1024 * 1024 + 1,
                size_basis=ResponseSizeBasis.DECLARED_CONTENT_LENGTH,
            ),
            "source_response_size_limit_exceeded",
            {
                "error_class": (
                    "app.collection.article_acquisition.errors."
                    "ResponseSizeLimitExceededError"
                ),
                "error_message": (
                    "source_response_size_limit_exceeded: observed=10485761"
                    " limit=10485760 basis=declared_content_length"
                ),
            },
        ),
    ],
)
async def test_partial_failure_is_logged_and_other_feeds_continue(
    error: Exception, code: str, exception_fields: dict[str, object], read_logs
) -> None:
    """一部のフィードの失敗は、取得元とフィードと分類のcodeに、変換した原因を添えて記録し、残りのフィードを続ける。"""
    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = [[_ENTRY], error, [_ENTRY]]
    result = await _collect(reader)
    assert len(result) == 2
    assert reader.fetch.await_count == 3
    failed = [log for log in read_logs() if log["event"] == "source_feed_fetch_failed"]
    assert failed == [
        {
            "event": "source_feed_fetch_failed",
            "level": "warning",
            "log_policy": "external_content_fetch",
            "source_name": str(Source.name),
            "feed_url": _FEEDS[1],
            "code": code,
            **exception_fields,
        }
    ]


@pytest.mark.asyncio
async def test_all_failures_preserve_every_exception_and_cause(read_logs) -> None:
    reader = AsyncMock(spec=RssReader)
    cause = ValueError("parser cause")
    first = UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
    )
    first.__cause__ = cause
    errors = [
        first,
        HttpResponseError(status_code=404, received_at=_RECEIVED),
        HttpResponseError(status_code=503, received_at=_RECEIVED),
    ]
    reader.fetch.side_effect = errors
    with pytest.raises(RssFeedErrors) as caught:
        await _collect(reader)
    assert [failure.feed_url for failure in caught.value.failures] == list(_FEEDS)
    assert [failure.error for failure in caught.value.failures] == errors
    assert caught.value.failures[0].error.__cause__ is cause
    assert reader.fetch.await_count == 3
    logs = read_logs()
    assert len([log for log in logs if log["event"] == "source_feed_fetch_failed"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_only", [True, False])
async def test_successful_empty_feed_counts_as_success(empty_only: bool) -> None:
    reader = AsyncMock(spec=RssReader)
    error = HttpResponseError(status_code=503, received_at=_RECEIVED)
    reader.fetch.side_effect = [[], [], []] if empty_only else [error, [], error]
    assert await _collect(reader) == []
    assert reader.fetch.await_count == 3


@pytest.mark.asyncio
async def test_unexpected_reader_failure_stops_before_later_feeds_and_selection(
    read_logs,
) -> None:
    class MustNotSelect(Source):
        @staticmethod
        def select(entries: list[RssEntry]) -> list[RssEntry]:
            pytest.fail("取得が完了しなければ候補を選択しない")

    reader = AsyncMock(spec=RssReader)
    error = RuntimeError("reader bug")
    reader.fetch.side_effect = [[_ENTRY], error, [_ENTRY]]
    with pytest.raises(RuntimeError) as caught:
        await _collect(reader, MustNotSelect)
    assert caught.value is error
    assert reader.fetch.await_count == 2
    assert not [
        log for log in read_logs() if log["event"] == "source_feed_fetch_failed"
    ]


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
        HttpResponseError(status_code=404, received_at=_RECEIVED),
    ],
)
async def test_selection_exception_is_not_caught_as_feed_failure(
    error: Exception, read_logs
) -> None:
    cause = ValueError("cause")

    class FailingSource(Source):
        @staticmethod
        def select(entries: list[RssEntry]) -> list[RssEntry]:
            raise error from cause

    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = [_ENTRY]
    with pytest.raises(type(error)) as caught:
        await _collect(reader, FailingSource)
    assert caught.value is error
    assert caught.value.__cause__ is cause
    assert reader.fetch.await_count == 3
    assert not [
        log for log in read_logs() if log["event"] == "source_feed_fetch_failed"
    ]


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
