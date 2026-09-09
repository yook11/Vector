"""本文採用と任意関数を新取得入口から検証する。"""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.fetcher import fetch_articles
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.sources.definitions.fierce_biotech import FierceBiotechSource
from app.collection.sources.definitions.meta_ai import MetaAISource
from app.collection.sources.definitions.microsoft_research import (
    MicrosoftResearchSource,
)
from app.collection.sources.definitions.plos_one import PLOSOneSource
from app.collection.sources.definitions.techcrunch import TechCrunchSource
from app.collection.sources.definitions.the_register import TheRegisterSource
from app.collection.sources.definitions.venturebeat import VentureBeatSource
from app.collection.sources.rss_acquisition import RssBodyPolicy, RssSource

_ENTRY = RssEntry(
    title="Title",
    link="https://example.com/news",
    guid=None,
    published=datetime(2026, 5, 1, tzinfo=UTC),
    summary=None,
    content_encoded=None,
    tags=(),
    raw_published=None,
    raw_updated=None,
)


async def _collect(source: RssSource, *entries: RssEntry) -> list[FetchedArticle]:
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = list(entries)
    return [item async for item in fetch_articles(source, ReaderTools(rss=reader))]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "content", "summary", "expected"),
    [
        (RssBodyPolicy.DISCARD, "body", "summary", None),
        (RssBodyPolicy.CONTENT_ENCODED, None, "summary", None),
        (RssBodyPolicy.CONTENT_ENCODED, "", "summary", None),
        (
            RssBodyPolicy.CONTENT_ENCODED,
            "<p>a</p><p>b<br>c &amp; d</p>",
            None,
            "a b c & d",
        ),
        (RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY, "aa", "bb", "aa"),
        (RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY, "<b>a</b>", "longer", "a"),
        (RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY, None, "summary", "summary"),
        (RssBodyPolicy.LONGEST_CONTENT_OR_SUMMARY, "", "", None),
        (RssBodyPolicy.CONTENT_OR_SUMMARY, "a", "longer", "a"),
        (RssBodyPolicy.CONTENT_OR_SUMMARY, None, "summary", "summary"),
        (RssBodyPolicy.CONTENT_OR_SUMMARY, "", "summary", "summary"),
        (RssBodyPolicy.CONTENT_OR_SUMMARY, "<p></p>", "summary", None),
        (RssBodyPolicy.CONTENT_OR_SUMMARY, None, None, None),
    ],
)
async def test_body_policy_preserves_raw_selection_and_plain_text(
    policy: RssBodyPolicy,
    content: str | None,
    summary: str | None,
    expected: str | None,
) -> None:
    # rawの選択後に空になっても別の候補を採り直さない。
    class Source(TechCrunchSource):
        acquisition = replace(TechCrunchSource.acquisition, body_policy=policy)

    result = await _collect(
        Source, replace(_ENTRY, content_encoded=content, summary=summary)
    )
    assert result[0].body == expected


@pytest.mark.asyncio
async def test_hooks_run_in_order_and_filtered_entries_are_not_transformed() -> None:
    trace: list[str] = []

    class Source(VentureBeatSource):
        @staticmethod
        def in_scope(entry: RssEntry) -> bool:
            trace.append(f"scope:{entry.title}")
            return entry.title != "drop"

        @staticmethod
        def transform_url(url: str) -> str:
            trace.append("url")
            assert url == _ENTRY.link
            return "https://example.com/transformed"

        @staticmethod
        def resolve_published_at(entry: RssEntry) -> datetime | None:
            trace.append("date")
            return None

        @staticmethod
        def transform_body(body: str) -> str:
            trace.append("body")
            assert body == "Hello & world"
            return ""

    result = await _collect(
        Source,
        replace(_ENTRY, content_encoded="<p>Hello &amp; world</p>"),
        replace(_ENTRY, title="drop"),
    )
    assert trace == ["scope:Title", "scope:drop", "url", "date", "body"]
    assert result == [
        FetchedArticle("Title", "https://example.com/transformed", None, None)
    ]


@pytest.mark.asyncio
async def test_discard_does_not_call_body_transform() -> None:
    class Source(TechCrunchSource):
        @staticmethod
        def transform_body(body: str) -> str:
            pytest.fail("DISCARD must not transform body")

    assert (await _collect(Source, _ENTRY))[0].body is None


@pytest.mark.asyncio
async def test_empty_adopted_body_still_reaches_transform() -> None:
    class Source(VentureBeatSource):
        @staticmethod
        def transform_body(body: str) -> str:
            assert body == ""
            return "replacement"

    assert (await _collect(Source, _ENTRY))[0].body == "replacement"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hook", ["in_scope", "transform_url", "resolve_published_at", "transform_body"]
)
async def test_hook_failure_preserves_exception_and_cause(
    hook: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cause = ValueError("original cause")
    error = RuntimeError("source hook failed")

    class Source(VentureBeatSource):
        pass

    def fail(value: object) -> object:
        raise error from cause

    monkeypatch.setattr(Source, hook, staticmethod(fail), raising=False)
    with pytest.raises(RuntimeError) as caught:
        await _collect(Source, _ENTRY)
    assert caught.value is error
    assert caught.value.__cause__ is cause
    frames = []
    traceback = caught.value.__traceback__
    while traceback:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "fail" in frames


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("published", "raw", "updated", "expected"),
    [
        (_ENTRY.published, "bad", None, _ENTRY.published),
        (None, "Apr 30, 2026 6:11pm", None, datetime(2026, 4, 30, 22, 11, tzinfo=UTC)),
        (None, None, "Jan 30, 2026 6:11pm", datetime(2026, 1, 30, 23, 11, tzinfo=UTC)),
        (None, "bad", "Apr 30, 2026 6:11pm", None),
        (None, None, None, None),
    ],
)
async def test_fierce_biotech_preserves_date_resolution(
    published: datetime | None,
    raw: str | None,
    updated: str | None,
    expected: datetime | None,
) -> None:
    result = await _collect(
        FierceBiotechSource,
        replace(_ENTRY, published=published, raw_published=raw, raw_updated=updated),
    )
    assert result[0].published_at == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tags", [("AI",), ("other", "AI"), ("ai",), (), ("Technology and Innovation",)]
)
async def test_meta_ai_requires_exact_ai_tag(tags: tuple[str, ...]) -> None:
    result = await _collect(MetaAISource, replace(_ENTRY, tags=tags))
    assert len(result) == (1 if "AI" in tags else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "url", "mode", "body"),
    [
        (VentureBeatSource, "https://venturebeat.com/feed", "text", "summary"),
        (PLOSOneSource, "https://journals.plos.org/plosone/feed/atom", "bytes", "c"),
        (
            MicrosoftResearchSource,
            "https://www.microsoft.com/en-us/research/feed/",
            "text",
            "c",
        ),
        (TheRegisterSource, "https://www.theregister.com/headlines.atom", "text", None),
        (FierceBiotechSource, "https://www.fiercebiotech.com/rss/xml", "text", None),
        (MetaAISource, "https://about.fb.com/news/feed/", "bytes", "summary"),
    ],
)
async def test_source_declaration_preserves_request_and_body_choice(
    source: RssSource,
    url: str,
    mode: str,
    body: str | None,
) -> None:
    reader = AsyncMock(spec=RssReader)
    reader.fetch.return_value = [
        replace(_ENTRY, content_encoded="c", summary="summary", tags=("AI",))
    ]
    result = [a async for a in fetch_articles(source, ReaderTools(rss=reader))]
    assert result[0].body == body
    reader.fetch.assert_awaited_once_with(
        endpoint_url=url, parse_mode=mode, source_name=str(source.name)
    )
