"""``ArticleUrl`` ORM の不変条件テスト (UNIQUE / FK / CHECK)。

DB レベルで強制されている不変条件を実 PG fixture (`db_session` + `sample_source`)
で確認する。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArticleUrl, NewsSource
from app.shared.value_objects.safe_url import SafeUrl


class TestArticleUrlPersistence:
    @pytest.mark.asyncio
    async def test_round_trip_persists_safe_url_value_objects(
        self, db_session: AsyncSession, sample_source: NewsSource
    ) -> None:
        url = ArticleUrl(
            normalized_url=SafeUrl("https://example.com/article"),
            original_url=SafeUrl("https://example.com/article?utm_source=x"),
            first_seen_source_id=sample_source.id,
        )
        db_session.add(url)
        await db_session.commit()
        await db_session.refresh(url)

        # SafeUrl で読み戻る (TypeDecorator が機能している)
        assert isinstance(url.normalized_url, SafeUrl)
        assert str(url.normalized_url) == "https://example.com/article"
        assert url.first_seen_at is not None
        assert url.id is not None


class TestArticleUrlConstraints:
    @pytest.mark.asyncio
    async def test_normalized_url_is_unique(
        self, db_session: AsyncSession, sample_source: NewsSource
    ) -> None:
        # 同じ normalized_url は 2 回 INSERT できない (URL 一意性 SSoT)
        first = ArticleUrl(
            normalized_url=SafeUrl("https://example.com/dup"),
            original_url=SafeUrl("https://example.com/dup"),
            first_seen_source_id=sample_source.id,
        )
        db_session.add(first)
        await db_session.commit()

        duplicate = ArticleUrl(
            normalized_url=SafeUrl("https://example.com/dup"),
            original_url=SafeUrl("https://example.com/dup?other=1"),
            first_seen_source_id=sample_source.id,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_first_seen_source_id_must_reference_existing(
        self, db_session: AsyncSession
    ) -> None:
        # FK 違反 (存在しない news_source への参照)
        url = ArticleUrl(
            normalized_url=SafeUrl("https://example.com/orphan"),
            original_url=SafeUrl("https://example.com/orphan"),
            first_seen_source_id=999_999_999,
        )
        db_session.add(url)
        with pytest.raises(IntegrityError):
            await db_session.commit()
