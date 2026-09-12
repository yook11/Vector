"""宣言から取得するRSS経路の出力・失敗・後段への接続を検証する。"""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import feedparser
import pytest

from app.collection.article_acquisition.errors import RssFeedErrors
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
    convert_fetched_article,
)
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.article_acquisition.reader.rss_reader import (
    RssEntry,
    RssReader,
    normalize_entry,
)
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedArticle
from app.collection.external_fetch_errors import FetchOriginServerError
from app.collection.sources.definitions.openai import OpenAISource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.rss_acquisition import (
    RssSource,
)

_PUBLISHED = datetime(2026, 5, 1, tzinfo=UTC)
_ENTRY = RssEntry(
    link="https://example.com/article/?utm_source=feed",
    title="Title",
    guid="https://example.com/different-guid",
    published=_PUBLISHED,
    summary="<p>" + "summary " * 100 + "</p>",
    content_encoded="<p>" + "body " * 100 + "</p>",
    tags=(),
    raw_published=None,
    raw_updated=None,
)
_CASES = [
    (TechCrunchSource, "https://techcrunch.com/feed/", "text", "techcrunch_rss.xml"),
    (OpenAISource, "https://openai.com/news/rss.xml", "bytes", "openai_rss.xml"),
]


@pytest.mark.parametrize(
    "overrides",
    [
        {"feeds": ()},
        {"feeds": ["https://example.com/feed"]},
        {"feeds": ("",)},
        {"feeds": (123,)},
        {"parse_mode": "auto"},
        {"body_policy": "discard"},
        {"body_policy": "content"},
    ],
)
def test_invalid_declaration_is_rejected(overrides: dict) -> None:
    # 型注釈を迂回した設定も通信前に拒否する。
    with pytest.raises(ValueError):
        replace(TechCrunchSource.acquisition, **overrides)


def test_declaration_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(TechCrunchSource.acquisition, "feeds", ())


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "url", "mode", "fixture"), _CASES)
async def test_declaration_controls_request_and_preserves_materials(
    source: RssSource, url: str, mode: str, fixture: str
) -> None:
    # 空値、不正URL、長いtitle、重複も取得側で裁かず後段へ渡す。
    entries = [
        _ENTRY,
        replace(_ENTRY, title="", link="", published=None),
        replace(_ENTRY, title="x" * 1000, link="javascript:bad"),
        _ENTRY,
    ]
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = entries
    result = [item async for item in fetch_articles(source, ReaderTools(rss=reader))]
    assert result == [
        FetchedArticle(title=e.title, url=e.link, body=None, published_at=e.published)
        for e in entries
    ]
    reader.fetch.assert_awaited_once_with(
        endpoint_url=url, source_name=str(source.name), parse_mode=mode
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "url", "mode", "fixture"), _CASES)
async def test_fixture_reaches_completion_as_observed_articles(
    source: RssSource, url: str, mode: str, fixture: str
) -> None:
    # 実フィードfixtureを本番の解析・変換へ通す。
    raw = (Path(__file__).parents[2] / "fixtures" / fixture).read_bytes()
    entries = [normalize_entry(e) for e in feedparser.parse(raw).entries]
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = entries
    fetched = [a async for a in fetch_articles(source, ReaderTools(rss=reader))]
    assert len(fetched) == len(entries) > 0
    converted = [
        convert_fetched_article(a, source=source, source_id=1) for a in fetched
    ]
    observed = [a for a in converted if isinstance(a, ObservedArticle)]
    rejected = [a for a in converted if isinstance(a, AcquisitionConversionRejection)]
    expected_counts = {"techcrunch_rss.xml": (2, 1), "openai_rss.xml": (3, 0)}
    assert (len(observed), len(rejected)) == expected_counts[fixture]
    assert len(observed) + len(rejected) == len(converted)
    assert all(
        a.outcome_code == "acquisition_conversion_title_missing" for a in rejected
    )
    assert all(a.body is None for a in fetched)


@pytest.mark.asyncio
async def test_missing_identity_is_visible_to_converter() -> None:
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = [replace(_ENTRY, link="")]
    fetched = [
        a async for a in fetch_articles(TechCrunchSource, ReaderTools(rss=reader))
    ]
    assert len(fetched) == 1
    assert isinstance(
        convert_fetched_article(fetched[0], source=TechCrunchSource, source_id=1),
        AcquisitionConversionRejection,
    )


@pytest.mark.asyncio
async def test_successful_empty_feed_remains_empty() -> None:
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = []
    assert [
        a async for a in fetch_articles(OpenAISource, ReaderTools(rss=reader))
    ] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        FetchOriginServerError(status_code=503, reason="unavailable"),
        UnreadableResponseError(
            reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
        ),
        RuntimeError("reader bug"),
    ],
)
async def test_fetch_failure_propagates_without_legacy_fallback(
    error: Exception,
) -> None:
    class SourceWithOldRead(TechCrunchSource):
        @staticmethod
        async def read(tools: ReaderTools) -> list[RssEntry]:
            pytest.fail("legacy read must not be used")

    reader = AsyncMock(spec=RssReader)
    reader.fetch.side_effect = error
    expected = RuntimeError if isinstance(error, RuntimeError) else RssFeedErrors
    with pytest.raises(expected) as caught:
        _ = [
            a async for a in fetch_articles(SourceWithOldRead, ReaderTools(rss=reader))
        ]
    if isinstance(caught.value, RssFeedErrors):
        assert len(caught.value.failures) == 1
        assert (
            caught.value.failures[0].feed_url == SourceWithOldRead.acquisition.feeds[0]
        )
        assert caught.value.failures[0].error is error
    else:
        assert caught.value is error
    reader.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_acquisition_does_not_fall_back_to_old_read() -> None:
    class InvalidSource:
        name = TechCrunchSource.name
        observed_origin = TechCrunchSource.observed_origin
        completion_policy = TechCrunchSource.completion_policy
        fetch_cadence = TechCrunchSource.fetch_cadence
        acquisition = None

        @staticmethod
        async def read(tools: ReaderTools) -> list[RssEntry]:
            pytest.fail("invalid declaration must not use legacy read")

    reader = AsyncMock(spec=RssReader)
    with pytest.raises(TypeError, match="unsupported acquisition"):
        _ = [a async for a in fetch_articles(InvalidSource, ReaderTools(rss=reader))]
    reader.fetch.assert_not_called()
