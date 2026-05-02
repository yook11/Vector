"""BriefingRepository の永続化挙動テスト (UPSERT / find / exists)。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.repository.briefings import BriefingRepository
from app.models.category import Category
from app.models.weekly_briefing import WeeklyBriefing


def _make(week: date, category_id: int, *, headline: str = "h1") -> WeeklyBriefing:
    return WeeklyBriefing(
        week_start_date=week,
        category_id=category_id,
        headline=headline,
        stories=[{"title": "s", "analysis": "a", "article_ids": [1]}],
        model_name="test-model",
        input_article_count=1,
    )


@pytest.fixture
async def category(db_session: AsyncSession) -> Category:
    cat = Category(slug="ai", name="AI")
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    return cat


class TestSave:
    @pytest.mark.asyncio
    async def test_inserts_new_row(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        saved = await repo.save(_make(date(2026, 4, 20), category.id))
        await db_session.commit()
        assert saved is not None
        assert saved.headline == "h1"
        assert saved.id > 0

    @pytest.mark.asyncio
    async def test_returns_none_on_conflict_without_force(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        first = await repo.save(_make(date(2026, 4, 20), category.id, headline="v1"))
        await db_session.commit()
        assert first is not None

        second = await repo.save(_make(date(2026, 4, 20), category.id, headline="v2"))
        await db_session.commit()
        assert second is None

        # 既存行は v1 のまま
        existing = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=category.id
        )
        assert existing is not None
        assert existing.headline == "v1"

    @pytest.mark.asyncio
    async def test_force_overwrites_existing(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        await repo.save(_make(date(2026, 4, 20), category.id, headline="v1"))
        await db_session.commit()

        forced = await repo.save(
            _make(date(2026, 4, 20), category.id, headline="v2"), force=True
        )
        await db_session.commit()
        assert forced is not None
        assert forced.headline == "v2"


class TestExists:
    @pytest.mark.asyncio
    async def test_false_when_missing(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        assert (
            await repo.exists(week_start=date(2026, 4, 20), category_id=category.id)
            is False
        )

    @pytest.mark.asyncio
    async def test_true_when_present(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        await repo.save(_make(date(2026, 4, 20), category.id))
        await db_session.commit()
        assert (
            await repo.exists(week_start=date(2026, 4, 20), category_id=category.id)
            is True
        )


class TestFindLatestByCategory:
    @pytest.mark.asyncio
    async def test_returns_most_recent(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        await repo.save(_make(date(2026, 4, 13), category.id, headline="old"))
        await repo.save(_make(date(2026, 4, 20), category.id, headline="latest"))
        await db_session.commit()

        latest = await repo.find_latest_by_category(category_id=category.id)
        assert latest is not None
        assert latest.headline == "latest"
        assert latest.week_start_date == date(2026, 4, 20)

    @pytest.mark.asyncio
    async def test_returns_none_when_empty(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        assert await repo.find_latest_by_category(category_id=category.id) is None
