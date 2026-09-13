"""実DBの現状態から取得依頼を組み立てる経路を検証する。"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.collection.sources.acquisition_request import (
    SourceAcquisitionSchedule,
)
from app.collection.sources.dispatch import (
    SourceDispatchRejectionCode,
    SourceDispatchService,
)


@pytest.mark.asyncio
async def test_requests_follow_current_active_registered_sources(system_database):
    """毎回のDB状態とcadenceに一致する依頼を返し、拒否と対象なしを区別する。"""
    async with system_database.connect("vector") as db:
        await db.execute("UPDATE news_sources SET is_active = false")
        rows = await db.fetch(
            "UPDATE news_sources SET is_active = true "
            "WHERE name IN ('TechCrunch', 'OpenAI') RETURNING id, name"
        )
        ids = {row["name"]: row["id"] for row in rows}
        assert set(ids) == {"TechCrunch", "OpenAI"}
        unknown_id = await db.fetchval(
            "INSERT INTO news_sources "
            "(name, source_type, site_url, endpoint_url, is_active) "
            "VALUES ('Dispatch Fixture', 'rss', 'https://fixture.invalid', "
            "'https://fixture.invalid/rss', true) RETURNING id"
        )

    engine = create_async_engine(system_database.url("vector_collect", sqlalchemy=True))
    try:
        dispatch = SourceDispatchService(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        schedule = SourceAcquisitionSchedule.model_validate(
            {"cadence": "high", "scheduled_at": "2026-09-13T10:00:00+09:00"}
        )
        selection = await dispatch.select(schedule.cadence)
        requests = tuple(
            schedule.create_request(source.id) for source in selection.targets
        )
        assert [request.to_message() for request in requests] == [
            {
                "request_id": f"high/2026-09-13T01:00:00Z/{ids['TechCrunch']}",
                "cadence": "high",
                "scheduled_at": "2026-09-13T01:00:00Z",
                "source_id": ids["TechCrunch"],
            }
        ]
        assert [
            (item.source_id, item.outcome_code) for item in selection.rejections
        ] == [(unknown_id, SourceDispatchRejectionCode.SOURCE_NOT_REGISTERED)]

        async with system_database.connect("vector") as db:
            await db.execute(
                "UPDATE news_sources SET is_active = false WHERE id = $1",
                ids["TechCrunch"],
            )
        selection = await dispatch.select(schedule.cadence)
        assert (
            tuple(schedule.create_request(source.id) for source in selection.targets)
            == ()
        )
        assert [item.source_id for item in selection.rejections] == [unknown_id]

        async with system_database.connect("vector") as db:
            await db.execute(
                "UPDATE news_sources SET is_active = false WHERE id = $1", unknown_id
            )
        selection = await dispatch.select(schedule.cadence)
        assert (
            tuple(schedule.create_request(source.id) for source in selection.targets)
            == ()
        )
        assert selection.rejections == ()
    finally:
        await engine.dispose()
