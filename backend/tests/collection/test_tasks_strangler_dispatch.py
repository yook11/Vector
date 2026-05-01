"""``fetch_source_metadata`` の Strangler dispatch テスト (Phase 1a' + 1b')。

新ルート対象 (VentureBeat / TechCrunch) は ``ingest_source.kiq`` に振り替え
られ、それ以外のソースは従来の ``SourceFetchService`` 経路に進むことを確認する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.ingestion import service as ingestion_service_mod
from app.collection.tasks import fetch_source_metadata, ingest_source
from app.models.news_source import NewsSource, SourceType


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


@pytest.fixture
async def techcrunch_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="TechCrunch",
        source_type=SourceType.RSS,
        site_url="https://techcrunch.com",
        endpoint_url="https://techcrunch.com/feed/",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def legacy_source(db_session: AsyncSession) -> NewsSource:
    """新ルートに含まれないソースの代表 (FierceBiotech)。

    PR-1b' で TechCrunch も新ルートに移ったため、旧 SourceFetchService 経路
    の例として別の Pattern H ソースを使う。
    """
    source = NewsSource(
        name="FierceBiotech",
        source_type=SourceType.RSS,
        site_url="https://www.fiercebiotech.com",
        endpoint_url="https://www.fiercebiotech.com/rss/xml",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


@pytest.mark.asyncio
async def test_dispatches_venturebeat_to_new_route(
    session_factory: async_sessionmaker[AsyncSession],
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VB の source_id が来ると ingest_source.kiq に振り替えられる。"""
    kiq_mock = AsyncMock()
    monkeypatch.setattr(ingest_source, "kiq", kiq_mock)

    result = await fetch_source_metadata(vb_source.id, ctx=_ctx(session_factory))

    assert result == {
        "source_id": vb_source.id,
        "status": "dispatched_new_route",
    }
    kiq_mock.assert_awaited_once_with(vb_source.id)


@pytest.mark.asyncio
async def test_dispatches_techcrunch_to_new_route(
    session_factory: async_sessionmaker[AsyncSession],
    techcrunch_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC (Pattern H) も新ルート対象として ingest_source.kiq に振り替えられる。"""
    kiq_mock = AsyncMock()
    monkeypatch.setattr(ingest_source, "kiq", kiq_mock)

    result = await fetch_source_metadata(
        techcrunch_source.id, ctx=_ctx(session_factory)
    )

    assert result == {
        "source_id": techcrunch_source.id,
        "status": "dispatched_new_route",
    }
    kiq_mock.assert_awaited_once_with(techcrunch_source.id)


@pytest.mark.asyncio
async def test_legacy_route_for_non_new_route_source(
    session_factory: async_sessionmaker[AsyncSession],
    legacy_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新ルート未登録 (FierceBiotech) は旧 SourceFetchService 経由で処理される。"""
    kiq_mock = AsyncMock()
    monkeypatch.setattr(ingest_source, "kiq", kiq_mock)

    # SourceFetchService.execute を stub (本物のフェッチを発生させないため)
    execute_mock = AsyncMock(
        return_value=ingestion_service_mod.SourceFetchedOutcome(new_discovered=[])
    )
    monkeypatch.setattr(
        ingestion_service_mod.SourceFetchService, "execute", execute_mock
    )

    result = await fetch_source_metadata(legacy_source.id, ctx=_ctx(session_factory))

    # 新ルートに振り替えられていない
    assert result["status"] != "dispatched_new_route"
    kiq_mock.assert_not_awaited()
    # 旧 service が呼ばれた
    execute_mock.assert_awaited_once_with(legacy_source.id)


@pytest.mark.asyncio
async def test_unknown_source_id_falls_through_to_legacy(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source 未存在の場合、新ルート判定を素通りして従来パスに進む。"""
    kiq_mock = AsyncMock()
    monkeypatch.setattr(ingest_source, "kiq", kiq_mock)

    execute_mock = AsyncMock(return_value=ingestion_service_mod.SourceNotFoundOutcome())
    monkeypatch.setattr(
        ingestion_service_mod.SourceFetchService, "execute", execute_mock
    )

    result = await fetch_source_metadata(999_999, ctx=_ctx(session_factory))

    assert result["status"] != "dispatched_new_route"
    kiq_mock.assert_not_awaited()
