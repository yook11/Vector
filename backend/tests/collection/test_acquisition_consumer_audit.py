"""取得 Consumer 経由の保存・取り消し・失敗監査を実 DB で確かめる。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition.consumer import ArticleAcquisitionConsumer
from app.collection.article_acquisition.errors import RssFeedErrors
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.article_acquisition.reader.rss_reader import RssEntry, RssReader
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_acquisition.strategy import SOURCES
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.persistence.analyzable_article_repository import (
    AnalyzableArticleRepository,
)
from app.collection.sources.acquisition_request import (
    SourceAcquisitionRequest,
    SourceAcquisitionSchedule,
)
from app.collection.sources.definitions.venturebeat import VentureBeatSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.http.destination_policy import HostBlockedError
from app.http.errors import HttpResponseError
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.incomplete_article import IncompleteArticle
from app.models.news_source import NewsSource, SourceType
from app.models.outbox_event import OutboxEvent
from app.models.pipeline_event import PipelineEvent


@pytest.fixture
async def vb_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="VentureBeat",
        source_type=SourceType.RSS,
        site_url="https://venturebeat.com",
        endpoint_url="https://venturebeat.com/feed/",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


def _request(source: NewsSource) -> SourceAcquisitionRequest:
    return SourceAcquisitionSchedule(
        cadence=FetchCadence.HIGH,
        scheduled_at=datetime(2026, 5, 1, tzinfo=UTC),
    ).create_request(source.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["Full article body. " * 30, "Short summary."])
async def test_later_rss_hook_failure_rolls_back_and_preserves_audit_cause(
    body: str,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """先行候補の実DB保存を取り消し、固有関数の失敗原因だけを監査に残す。"""
    saved_ids: list[int] = []
    cause = ValueError("invalid footer")
    error = RuntimeError("body transform failed")
    repository = (
        AnalyzableArticleRepository if len(body) > 100 else IncompleteArticleRepository
    )
    original_save = repository.save

    async def save_and_record(self: Any, *args: Any, **kwargs: Any) -> int | None:
        saved_id = await original_save(self, *args, **kwargs)
        assert saved_id is not None
        saved_ids.append(saved_id)
        return saved_id

    monkeypatch.setattr(repository, "save", save_and_record)

    class FailingSource(VentureBeatSource):
        acquisition = replace(
            VentureBeatSource.acquisition,
            feeds=("https://venturebeat.com/a/feed", "https://venturebeat.com/b/feed"),
        )

        @staticmethod
        def transform_body(value: str) -> str:
            if value == "fail":
                assert len(saved_ids) == 1
                raise error from cause
            return value

    first = RssEntry(
        link="https://venturebeat.com/ai/first",
        title="First article",
        guid=None,
        published=datetime(2026, 5, 1, tzinfo=UTC),
        summary=None,
        content_encoded=body,
        tags=(),
        raw_published=None,
        raw_updated=None,
    )
    reader = AsyncMock(
        side_effect=[
            [first],
            [
                replace(
                    first,
                    link="https://venturebeat.com/ai/second",
                    content_encoded="fail",
                )
            ],
        ]
    )
    monkeypatch.setattr(RssReader, "fetch", reader)
    monkeypatch.setitem(SOURCES, VentureBeatSource.name, FailingSource)

    with pytest.raises(RuntimeError) as caught:
        await ArticleAcquisitionConsumer(session_factory, ReaderTools).consume(
            _request(vb_source)
        )

    assert caught.value is error
    assert caught.value.__cause__ is cause
    assert len(saved_ids) == 1
    for model in (AnalyzableArticleRecord, IncompleteArticle):
        assert not (
            await db_session.scalars(
                select(model.id).where(model.source_id == vb_source.id)
            )
        ).all()
    assert not (
        await db_session.scalars(
            select(OutboxEvent.event_id).where(
                OutboxEvent.payload["source_id"].as_integer() == vb_source.id
            )
        )
    ).all()
    events = (
        await db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.source_id == vb_source.id)
        )
    ).all()
    assert len(events) == 1
    row = events[0]
    assert row.event_type == "failed"
    assert row.outcome_code == "unexpected_error"
    assert row.error_class == "builtins.RuntimeError"
    assert row.payload["error_message"] == "body transform failed"
    assert row.payload["error_chain"] == [
        "builtins.RuntimeError",
        "builtins.ValueError",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["partial_success", "all_failed", "all_failed_retryable", "selection_failed"],
)
async def test_multi_feed_acquisition_preserves_persistence_and_failure_audit(
    scenario: str,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分成功は保存し、取得・選択の全体失敗は未保存のまま原因を監査する。"""
    cause = ValueError("parse cause")
    read_error = UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
    )
    read_error.__cause__ = cause
    selection_error = RuntimeError("selection failed")

    class MultiSource(VentureBeatSource):
        acquisition = replace(
            VentureBeatSource.acquisition,
            feeds=("https://venturebeat.com/a/feed", "https://venturebeat.com/b/feed"),
        )

        @staticmethod
        def select(entries: list[RssEntry]) -> list[RssEntry]:
            if scenario == "selection_failed":
                raise selection_error from cause
            return entries

    full = RssEntry(
        title="Full article",
        link="https://venturebeat.com/ai/full",
        guid=None,
        published=datetime(2026, 5, 1, tzinfo=UTC),
        summary=None,
        content_encoded="Full article body. " * 30,
        tags=(),
        raw_published=None,
        raw_updated=None,
    )
    short = replace(
        full,
        title="Short article",
        link="https://venturebeat.com/ai/short",
        content_encoded="Short summary.",
    )
    all_failed = scenario.startswith("all_failed")
    second_error = (
        HttpResponseError(
            status_code=503, received_at=datetime(2026, 9, 26, tzinfo=UTC)
        )
        if scenario == "all_failed_retryable"
        else HostBlockedError("blocked")
    )
    reader = AsyncMock(
        side_effect=(
            [read_error, second_error]
            if all_failed
            else [[full, short], read_error if scenario == "partial_success" else []]
        )
    )
    monkeypatch.setattr(RssReader, "fetch", reader)
    monkeypatch.setitem(SOURCES, VentureBeatSource.name, MultiSource)
    consumer = ArticleAcquisitionConsumer(session_factory, ReaderTools)
    if scenario == "selection_failed":
        with pytest.raises(RuntimeError) as caught:
            await consumer.consume(_request(vb_source))
        assert caught.value is selection_error
        assert caught.value.__cause__ is cause
    elif all_failed:
        with pytest.raises(RssFeedErrors):
            await consumer.consume(_request(vb_source))
    else:
        result = await consumer.consume(_request(vb_source))
        assert result.result == "acquired"
    assert reader.await_count == 2

    article_ids = (
        await db_session.scalars(
            select(AnalyzableArticleRecord.id).where(
                AnalyzableArticleRecord.source_id == vb_source.id
            )
        )
    ).all()
    incomplete_ids = (
        await db_session.scalars(
            select(IncompleteArticle.id).where(
                IncompleteArticle.source_id == vb_source.id
            )
        )
    ).all()
    outbox = (await db_session.scalars(select(OutboxEvent.event_type))).all()
    events = (
        await db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.source_id == vb_source.id)
        )
    ).all()
    if scenario == "partial_success":
        assert len(article_ids) == len(incomplete_ids) == 1
        assert sorted(outbox) == [
            "article.analyzable_created",
            "article.incomplete_recorded",
        ]
        assert sorted(row.outcome_code for row in events) == [
            "article_created",
            "incomplete_article_created",
        ]
    else:
        assert not article_ids and not incomplete_ids and not outbox
        assert len(events) == 1
        row = events[0]
        assert row.event_type == "failed"
        assert row.outcome_code == (
            "rss_feed_errors" if all_failed else "unexpected_error"
        )
        if all_failed:
            assert row.error_class.endswith(".RssFeedErrors")
            assert row.retryability == (
                "retryable" if scenario == "all_failed_retryable" else "non_retryable"
            )
            failures = row.payload["feed_failures"]
            assert [f["feed_url"] for f in failures] == list(
                MultiSource.acquisition.feeds
            )
            assert [(f["code"], f["http_status"]) for f in failures] == (
                [("read_malformed_content", None), ("http_response_error", 503)]
                if scenario == "all_failed_retryable"
                else [("read_malformed_content", None), ("host_blocked", None)]
            )
            assert failures[0]["error_chain"][-1] == "builtins.ValueError"
            assert failures[0]["error_class"].endswith(".UnreadableResponseError")
            assert failures[1]["error_class"].endswith(
                f".{type(second_error).__name__}"
            )
        else:
            assert row.payload["error_chain"][-1] == "builtins.ValueError"
            assert row.payload["feed_failures"] is None
