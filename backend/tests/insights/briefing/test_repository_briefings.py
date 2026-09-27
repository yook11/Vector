"""BriefingRepository の永続化挙動テスト (save / find / exists)。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.briefing import (
    BriefingChapter,
    KeyArticle,
    WatchPoint,
    WeeklyBriefingContent,
)
from app.insights.briefing.repository import BriefingRepository
from app.models.category import Category


def _content(
    *,
    headline: str = "h1",
    summary: str = "今週の総括",
    chapters: list[BriefingChapter] | None = None,
) -> WeeklyBriefingContent:
    """テスト用の最小 WeeklyBriefingContent を組み立てる。"""
    return WeeklyBriefingContent(
        headline=headline,
        summary=summary,
        chapters=chapters or [BriefingChapter(heading="資金とインフラ", body="章本文")],
        key_articles=[KeyArticle(analyzed_article_id=1, significance="なぜ重要か")],
        watch_points=[WatchPoint(statement="今後どこを見るべきか")],
    )


_SAVE_KWARGS: dict = dict(
    week_start=date(2026, 4, 20),
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
        saved = await repo.save(_content(), category_id=category.id, **_SAVE_KWARGS)
        await db_session.commit()
        assert saved is not None
        assert saved.headline == "h1"
        assert saved.id > 0

    @pytest.mark.asyncio
    async def test_vo_fields_persisted_to_orm_row(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        """save が WeeklyBriefingContent の全フィールドを ORM 行へ写像する。

        VO→行の写像は repository 内部の責務 (service が手組みしない)。
        key_articles の永続形は {analyzed_article_id, significance}。
        domain/LLM 語彙 analyzed_article_id を repository 境界でも保持し、
        値は公開 /news id 空間 (AnalyzedArticleRecord.id) のまま保持する。
        """
        content = _content(headline="mapped", summary="マップ確認")
        repo = BriefingRepository(db_session)
        saved = await repo.save(content, category_id=category.id, **_SAVE_KWARGS)
        await db_session.commit()
        assert saved is not None
        assert saved.headline == content.headline
        assert saved.summary == content.summary
        assert saved.chapters == [c.model_dump() for c in content.chapters]
        # key_articles は repository が domain 語彙 analyzed_article_id を
        # そのまま永続化する (旧形 {analyzed_article_id} とはキー名で判別できる)。
        assert saved.key_articles == [
            {
                "analyzed_article_id": a.analyzed_article_id,
                "significance": a.significance,
            }
            for a in content.key_articles
        ]
        assert saved.watch_points == [w.model_dump() for w in content.watch_points]
        assert saved.model_name == _SAVE_KWARGS["model_name"]
        assert saved.input_article_count == _SAVE_KWARGS["input_article_count"]

    @pytest.mark.asyncio
    async def test_returns_none_on_conflict(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        first = await repo.save(
            _content(headline="v1"), category_id=category.id, **_SAVE_KWARGS
        )
        await db_session.commit()
        assert first is not None

        second = await repo.save(
            _content(headline="v2"), category_id=category.id, **_SAVE_KWARGS
        )
        await db_session.commit()
        assert second is None

        # 既存行は v1 のまま (副作用なし)
        existing = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=category.id
        )
        assert existing is not None
        assert existing.headline == "v1"


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
        await repo.save(_content(), category_id=category.id, **_SAVE_KWARGS)
        await db_session.commit()
        assert (
            await repo.exists(week_start=date(2026, 4, 20), category_id=category.id)
            is True
        )
