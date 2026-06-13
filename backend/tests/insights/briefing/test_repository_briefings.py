"""BriefingRepository の永続化挙動テスト (UPSERT / find / exists)。"""

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
        key_articles=[KeyArticle(article_id=1, significance="なぜ重要か")],
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
        key_articles の永続形は {assessment_id, significance} (新形)。
        domain 語彙 article_id (LLM 契約) は repository 境界で assessment_id へ
        改名し、値は公開 /news id 空間 (InScopeAssessment.id) のまま保持する。
        """
        content = _content(headline="mapped", summary="マップ確認")
        repo = BriefingRepository(db_session)
        saved = await repo.save(content, category_id=category.id, **_SAVE_KWARGS)
        await db_session.commit()
        assert saved is not None
        assert saved.headline == content.headline
        assert saved.summary == content.summary
        assert saved.chapters == [c.model_dump() for c in content.chapters]
        # key_articles は repository が domain 語彙 article_id を assessment_id へ
        # 写像して永続化する (旧形 {article_id} とはキー名で判別できる)。
        assert saved.key_articles == [
            {"assessment_id": a.article_id, "significance": a.significance}
            for a in content.key_articles
        ]
        assert saved.watch_points == [w.model_dump() for w in content.watch_points]
        assert saved.model_name == _SAVE_KWARGS["model_name"]
        assert saved.input_article_count == _SAVE_KWARGS["input_article_count"]

    @pytest.mark.asyncio
    async def test_returns_none_on_conflict_without_force(
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

    @pytest.mark.asyncio
    async def test_force_overwrites_existing(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        await repo.save(
            _content(headline="v1", summary="s1"),
            category_id=category.id,
            **_SAVE_KWARGS,
        )
        await db_session.commit()

        new_chapters = [BriefingChapter(heading="新章", body="新本文")]
        forced = await repo.save(
            _content(headline="v2", summary="s2", chapters=new_chapters),
            category_id=category.id,
            **_SAVE_KWARGS,
            force=True,
        )
        await db_session.commit()
        assert forced is not None
        assert forced.headline == "v2"
        assert forced.summary == "s2"
        assert forced.chapters == [{"heading": "新章", "body": "新本文"}]

    @pytest.mark.asyncio
    async def test_force_updates_generated_at_and_updated_at(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        """force=True の upsert は generated_at / updated_at を NOW() に更新する。"""
        repo = BriefingRepository(db_session)
        first = await repo.save(
            _content(headline="v1"), category_id=category.id, **_SAVE_KWARGS
        )
        await db_session.commit()
        assert first is not None
        original_generated_at = first.generated_at
        original_updated_at = first.updated_at

        forced = await repo.save(
            _content(headline="v2"),
            category_id=category.id,
            **_SAVE_KWARGS,
            force=True,
        )
        await db_session.commit()
        assert forced is not None
        # force upsert は generated_at / updated_at を更新する
        assert forced.generated_at >= original_generated_at
        assert forced.updated_at >= original_updated_at


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


class TestFindLatestByCategory:
    @pytest.mark.asyncio
    async def test_returns_most_recent(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        await repo.save(
            _content(headline="old"),
            category_id=category.id,
            week_start=date(2026, 4, 13),
            model_name="test-model",
            input_article_count=1,
        )
        await repo.save(
            _content(headline="latest"),
            category_id=category.id,
            week_start=date(2026, 4, 20),
            model_name="test-model",
            input_article_count=1,
        )
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


class TestFindLatestForEachCategory:
    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_none(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        repo = BriefingRepository(db_session)
        assert await repo.find_latest_for_each_category() == {}

    @pytest.mark.asyncio
    async def test_returns_latest_per_category_with_mixed_history(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        # 別カテゴリも追加して 2 行入れる (古い行 + 新しい行)
        other = Category(slug="robotics", name="ロボティクス")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)

        repo = BriefingRepository(db_session)
        # ai: 古い + 新しい
        await repo.save(
            _content(headline="ai-old"),
            category_id=category.id,
            week_start=date(2026, 4, 13),
            model_name="test-model",
            input_article_count=1,
        )
        await repo.save(
            _content(headline="ai-latest"),
            category_id=category.id,
            week_start=date(2026, 4, 20),
            model_name="test-model",
            input_article_count=1,
        )
        # robotics: 1 行のみ
        await repo.save(
            _content(headline="robotics-only"),
            category_id=other.id,
            week_start=date(2026, 4, 13),
            model_name="test-model",
            input_article_count=1,
        )
        await db_session.commit()

        result = await repo.find_latest_for_each_category()

        assert set(result.keys()) == {category.id, other.id}
        assert result[category.id].headline == "ai-latest"
        assert result[category.id].week_start_date == date(2026, 4, 20)
        assert result[other.id].headline == "robotics-only"

    @pytest.mark.asyncio
    async def test_skips_categories_without_briefing(
        self, db_session: AsyncSession, category: Category
    ) -> None:
        """生成されていないカテゴリは dict に entry が無いこと。"""
        other = Category(slug="bio", name="バイオ")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)

        repo = BriefingRepository(db_session)
        await repo.save(_content(), category_id=category.id, **_SAVE_KWARGS)
        await db_session.commit()

        result = await repo.find_latest_for_each_category()

        assert category.id in result
        assert other.id not in result
