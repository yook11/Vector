"""``ingest_source`` task の例外パス監査テスト。

Stage 1 設計 (cron 一本化、taskiq inline retry なし) における Service 例外の
ハンドリングを検証する:

- ``SourceFetchError`` (Stage 1 共通基底) → audit + return、taskiq retry なし
- ``PermanentFetchError`` / ``TemporaryFetchError`` (Stage 2 専用 subclass) も
  ``SourceFetchError`` の subclass なので Stage 1 task で catch される
- 想定外 ``Exception`` → audit + re-raise (worker log で可視化)

Stage 1 では ``max_retries=0 / retry_on_error=False`` のため、attempt は常に 1。
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
    SourceFetchError,
    TemporaryFetchError,
)
from app.collection.staged import IngestSourceArg
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


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    """taskiq Context の最低限な mock。

    Stage 1 は retry concept を持たないため labels は空で十分。
    """
    state = SimpleNamespace(session_factory=session_factory)
    message = SimpleNamespace(labels={})
    return SimpleNamespace(state=state, message=message)


class _RaisingService:
    """指定された例外を raise する ArticleAcquisitionService スタンド。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def execute(self, source_id: int) -> Any:
        raise self.exc  # type: ignore[attr-defined]


def _patch_service_to_raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    cls = type(
        "_S",
        (_RaisingService,),
        {"exc": exc},
    )
    monkeypatch.setattr(
        "app.collection.service.ArticleAcquisitionService",
        cls,
    )


@pytest.mark.asyncio
async def test_source_fetch_error_records_audit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 1 共通基底の ``SourceFetchError`` で audit + return される。"""
    _patch_service_to_raise(monkeypatch, SourceFetchError("ssrf blocked"))
    ctx = _ctx(session_factory)

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
    assert row.outcome_code == "source_fetch_error"
    assert row.source_id == vb_source.id
    assert row.attempt == 1
    assert row.error_class.endswith(".SourceFetchError")  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        PermanentFetchError("403 forbidden"),
        TemporaryFetchError("503"),
    ],
    ids=["permanent", "temporary"],
)
async def test_stage2_subclasses_caught_as_source_fetch_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
) -> None:
    """Stage 2 専用語彙 (``PermanentFetchError`` / ``TemporaryFetchError``) も
    ``SourceFetchError`` subclass なので Stage 1 task で catch される。

    Fetcher 実装は依然これらを raise するため、Stage 1 で subclass 軸を区別せず
    catch できる構造を保証する。
    """
    _patch_service_to_raise(monkeypatch, exc)
    ctx = _ctx(session_factory)

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
    assert row.outcome_code == "source_fetch_error"
    assert row.attempt == 1


@pytest.mark.asyncio
async def test_unexpected_error_records_then_reraises(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SourceFetchError`` 外の Exception は audit + re-raise (worker log 可視化)。"""
    _patch_service_to_raise(monkeypatch, RuntimeError("boom"))
    ctx = _ctx(session_factory)

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
    assert row.attempt == 1
    assert row.error_class.endswith(".RuntimeError")  # type: ignore[union-attr]
