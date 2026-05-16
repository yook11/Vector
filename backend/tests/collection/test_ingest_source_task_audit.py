"""``ingest_source`` task の例外パス監査テスト。

Stage 1 設計 (cron 一本化、taskiq inline retry なし) における Service 例外の
task 層配線を検証する。audit row の CODE / category / payload 不変条件の網羅は
``test_source_fetch_failure_dispatch`` が担うため、本 file は task の分岐配線
(catch → _record_fetch_log → handler dispatch → return / reraise) に集中する:

- ``SourceFetchError`` (Layer 1 marker) → audit + return、taskiq retry なし。
  ``pipeline_events.code`` に origin CODE がそのまま入り SQL 可能になる
  (``category`` は collection stage なので NULL、payload に code を二重焼きしない)。
- 想定外 ``Exception`` → audit + re-raise (worker log で可視化、code は
  ``unexpected_error``)。

Stage 1 では ``max_retries=0 / retry_on_error=False`` のため、attempt は常に 1。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection import tasks as collection_tasks
from app.collection.source_fetch.errors import SourceFetchError
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
        "app.collection.source_fetch.service.ArticleAcquisitionService",
        cls,
    )


async def _failed_event(db_session: AsyncSession) -> PipelineEvent:
    return (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "failed")
            )
        )
        .scalars()
        .one()
    )


@pytest.mark.asyncio
async def test_source_fetch_error_records_origin_code_and_returns(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SourceFetchError`` → origin CODE で audit + error dict を return。

    ``pipeline_events.code`` / ``outcome_code`` に marker の origin CODE が
    そのまま入り、``category`` は collection stage なので NULL、``payload`` に
    ``code`` を二重に焼かない (state は top-level 軸で識別する)。
    """
    _patch_service_to_raise(
        monkeypatch,
        SourceFetchError("ssrf blocked: 10.0.0.1", code="fetch_ssrf_blocked"),
    )
    ctx = _ctx(session_factory)

    result = await collection_tasks.ingest_source(
        IngestSourceArg(id=vb_source.id, name=str(vb_source.name)),
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    row = await _failed_event(db_session)
    assert row.code == "fetch_ssrf_blocked"
    assert row.outcome_code == "fetch_ssrf_blocked"
    assert row.category is None
    assert "code" not in row.payload
    assert row.source_id == vb_source.id
    assert row.attempt == 1
    assert row.error_class.endswith(".SourceFetchError")  # type: ignore[union-attr]


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

    row = await _failed_event(db_session)
    assert row.code == "unexpected_error"
    assert row.outcome_code == "unexpected_error"
    assert row.category is None
    assert row.attempt == 1
    assert row.error_class.endswith(".RuntimeError")  # type: ignore[union-attr]
