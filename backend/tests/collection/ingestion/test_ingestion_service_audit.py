"""``IngestionService`` の同 tx 監査書込テスト。

検証する不変条件:

- 成功 path で ``pipeline_events`` に 1 行が書き込まれる (Service / Task の
  ``attempt`` がそのまま行に載る)
- ``Failed`` の集計 (``failed_codes``) が payload に焼き付く
- Fetcher が運んだ ``metadata`` の key 集合 / 最初の non-empty entry の dump が
  payload (``metadata_fields_observed`` / ``metadata_sample``) に焼き付く
- 全 entry の metadata が空のときは observation も None になる (一貫した null 表現)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.ingestion.domain.fetched_article import (
    Failed,
    FailureReason,
    FetchedEntry,
    FetchOutcome,
    ReadyForArticle,
)
from app.collection.ingestion.ingestion_service import IngestionService
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from app.shared.value_objects.safe_url import SafeUrl


def _entry(
    source_id: int,
    url: str,
    metadata: dict[str, object] | None = None,
) -> FetchedEntry:
    return FetchedEntry(
        item=ReadyForArticle(
            title="T",
            body="x" * 100,
            published_at=PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC)),
            source_id=source_id,
            source_url=SafeUrl(url),
        ),
        metadata=metadata if metadata is not None else {"language": "en-US"},
    )


class _StubFetcher:
    PROVIDES: ClassVar[frozenset[str]] = frozenset({"language", "site_name"})

    def __init__(self, outcomes: list[FetchOutcome]) -> None:
        self._outcomes = outcomes

    async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]:
        for o in self._outcomes:
            yield o


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


@pytest.mark.asyncio
async def test_success_writes_one_pipeline_event(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher([_entry(vb_source.id, "https://venturebeat.com/a/")]),
    )

    await svc.execute(vb_source.id, attempt=2)

    e = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert e.stage == "source_fetch"
    assert e.event_type == "succeeded"
    assert e.source_id == vb_source.id
    assert e.attempt == 2
    assert e.duration_ms is not None and e.duration_ms >= 0
    assert e.payload["persisted_count"] == 1


@pytest.mark.asyncio
async def test_failed_codes_aggregated_in_payload(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Failed.reason.code 別カウントが payload に焼かれ、後で監視できる。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [
                Failed(reason=FailureReason(code="body_too_short", retryable=False)),
                Failed(reason=FailureReason(code="title_missing", retryable=False)),
                Failed(reason=FailureReason(code="body_too_short", retryable=False)),
            ]
        ),
    )

    await svc.execute(vb_source.id)

    e = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert e.payload["persisted_count"] == 0
    assert e.payload["failed_count"] == 3
    assert e.payload["failed_codes"] == {"body_too_short": 2, "title_missing": 1}


@pytest.mark.asyncio
async def test_metadata_observation_records_keys_and_first_sample(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """全 entry の metadata key 累積 + 最初の non-empty dump が焼かれる。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [
                _entry(
                    vb_source.id,
                    "https://venturebeat.com/a/",
                    metadata={"language": "en-US", "site_name": "VentureBeat"},
                ),
                _entry(
                    vb_source.id,
                    "https://venturebeat.com/b/",
                    metadata={"language": "en-US", "guid": "abc"},
                ),
            ]
        ),
    )

    await svc.execute(vb_source.id)

    e = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert e.payload["metadata_fields_observed"] == ["guid", "language", "site_name"]
    assert e.payload["metadata_sample"] == {
        "language": "en-US",
        "site_name": "VentureBeat",
    }


@pytest.mark.asyncio
async def test_metadata_observation_is_none_when_all_empty(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """metadata が全 entry で空なら observation も None で焼く (一貫した欠落表現)。"""
    svc = IngestionService(
        session_factory,
        lambda: _StubFetcher(
            [_entry(vb_source.id, "https://venturebeat.com/a/", metadata={})]
        ),
    )

    await svc.execute(vb_source.id)

    e = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert e.payload["metadata_fields_observed"] is None
    assert e.payload["metadata_sample"] is None
