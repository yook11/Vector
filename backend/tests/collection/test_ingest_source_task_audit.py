"""``ingest_source`` task の例外パス監査テスト (PR1)。

3 種の except (Permanent / Temporary 最終 / Unexpected) で
``_record_failure_event`` が ``pipeline_events`` に failed 行を書くこと、
attempt が ``retry_count + 1`` で渡ること、Temporary 中間試行では audit
されないことを確認する。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection import tasks as collection_tasks
from app.collection.errors import (
    PermanentFetchError,
    TemporaryFetchError,
)
from app.collection.ingestion.staged import IngestSourceArg
from app.models.news_source import NewsSource, SourceType
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


def _ctx(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retry_count: int,
    max_retries: int = 2,
) -> SimpleNamespace:
    """taskiq Context の最低限な mock。

    ``is_last_attempt(ctx)`` と ``ctx.state.session_factory`` 経由で参照される。
    """
    state = SimpleNamespace(session_factory=session_factory)
    message = SimpleNamespace(
        labels={"retry_count": retry_count, "max_retries": max_retries}
    )
    return SimpleNamespace(state=state, message=message)


class _RaisingService:
    """指定された例外を raise する IngestionService スタンド。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def execute(self, source_id: int, *, attempt: int = 1) -> Any:
        raise self.exc  # type: ignore[attr-defined]


def _patch_service_to_raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    cls = type(
        "_S",
        (_RaisingService,),
        {"exc": exc},
    )
    monkeypatch.setattr(
        "app.collection.ingestion.ingestion_service.IngestionService",
        cls,
    )


@pytest.mark.asyncio
async def test_permanent_fetch_error_records_audit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_to_raise(monkeypatch, PermanentFetchError("403 forbidden"))
    ctx = _ctx(session_factory, retry_count=0)

    result = await collection_tasks.ingest_source(
        IngestSourceArg(id=vb_source.id, name=str(vb_source.name)),
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "failed")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome_code == "permanent_fetch_error"
    assert row.source_id == vb_source.id
    assert row.attempt == 1  # retry_count(0) + 1
    assert row.error_class.endswith(".PermanentFetchError")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_temporary_fetch_error_intermediate_does_not_record(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中間試行は audit せず、taskiq retry に乗せる (raise)。"""
    _patch_service_to_raise(monkeypatch, TemporaryFetchError("503"))
    ctx = _ctx(session_factory, retry_count=0, max_retries=2)  # 最終ではない

    with pytest.raises(TemporaryFetchError):
        await collection_tasks.ingest_source(
            IngestSourceArg(id=vb_source.id, name=str(vb_source.name)),
            ctx=ctx,  # type: ignore[arg-type]
        )

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "failed")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_temporary_fetch_error_last_attempt_records(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_to_raise(monkeypatch, TemporaryFetchError("503"))
    ctx = _ctx(session_factory, retry_count=2, max_retries=2)  # 最終試行

    result = await collection_tasks.ingest_source(
        IngestSourceArg(id=vb_source.id, name=str(vb_source.name)),
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    row = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "failed")
            )
        )
        .scalars()
        .one()
    )
    assert row.outcome_code == "temporary_fetch_error_exhausted"
    assert row.attempt == 3  # retry_count(2) + 1


@pytest.mark.asyncio
async def test_unexpected_error_records_then_reraises(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_service_to_raise(monkeypatch, RuntimeError("boom"))
    ctx = _ctx(session_factory, retry_count=0)

    with pytest.raises(RuntimeError, match="boom"):
        await collection_tasks.ingest_source(
            IngestSourceArg(id=vb_source.id, name=str(vb_source.name)),
            ctx=ctx,  # type: ignore[arg-type]
        )

    row = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "failed")
            )
        )
        .scalars()
        .one()
    )
    assert row.outcome_code == "unexpected_error"
    assert row.error_class.endswith(".RuntimeError")  # type: ignore[union-attr]
