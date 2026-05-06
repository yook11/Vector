"""``PendingHtmlArticle`` ORM の不変条件テスト (CHECK / UNIQUE / FK)。

state model の整合性 (status × leased_until の組合せ / ready_at 必須等) が
DB レベルで強制されていることを実 PG fixture で確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NewsSource, PendingHtmlArticle
from app.shared.value_objects.safe_url import SafeUrl


def _now() -> datetime:
    return datetime.now(UTC)


def _url(suffix: str = "") -> SafeUrl:
    return SafeUrl(f"https://example.com/pending{suffix}")


class TestPendingHtmlArticleHappyPath:
    @pytest.mark.asyncio
    async def test_open_with_ready_at_persists(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        # 通常の open 行: ready_at 必須、leased_until は NULL
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="open",
            staged_attributes={"title": "T"},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(pending)
        await db_session.commit()
        await db_session.refresh(pending)

        assert pending.id is not None
        assert pending.attempt_count == 0
        assert pending.staged_attributes == {"title": "T"}

    @pytest.mark.asyncio
    async def test_running_with_leased_until_persists(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        # claim 直後の running: leased_until が値を持ち、ready_at も値を持つ
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="running",
            staged_attributes={},
            ready_at=_now(),
            leased_until=_now() + timedelta(minutes=5),
            attempt_count=1,
        )
        db_session.add(pending)
        await db_session.commit()
        assert pending.id is not None

    @pytest.mark.asyncio
    async def test_closed_allows_null_ready_at(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        # closed は再試行しないので ready_at NULL でも OK
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="closed",
            staged_attributes={},
            ready_at=None,
            leased_until=None,
        )
        db_session.add(pending)
        await db_session.commit()
        assert pending.id is not None


class TestPendingHtmlArticleStateConsistency:
    @pytest.mark.asyncio
    async def test_open_with_leased_until_rejected(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        # open なのに leased_until が残っている = state 不整合
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="open",
            staged_attributes={},
            ready_at=_now(),
            leased_until=_now() + timedelta(minutes=5),
        )
        db_session.add(pending)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_running_without_leased_until_rejected(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        # running なのに leased_until NULL = claim 整合性違反
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="running",
            staged_attributes={},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(pending)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_open_without_ready_at_rejected(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        # open なのに ready_at NULL だと picking から永久に漏れる事故
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="open",
            staged_attributes={},
            ready_at=None,
            leased_until=None,
        )
        db_session.add(pending)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        pending = PendingHtmlArticle(
            url=_url(),
            source_id=sample_source.id,
            status="zombie",
            staged_attributes={},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(pending)
        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestPendingHtmlArticleUniqueness:
    @pytest.mark.asyncio
    async def test_url_is_unique(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        """``url`` 列の UNIQUE が cross-table dedup の物理保証を担う。"""
        shared = SafeUrl("https://example.com/shared")
        first = PendingHtmlArticle(
            url=shared,
            source_id=sample_source.id,
            status="open",
            staged_attributes={},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(first)
        await db_session.commit()

        duplicate = PendingHtmlArticle(
            url=shared,
            source_id=sample_source.id,
            status="open",
            staged_attributes={},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_url_check_constraint_rejects_non_http_scheme(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        """``url ~ '^https?://.+'`` CHECK が非 http(s) を遮断する。

        SafeUrl VO の同値ガードをすり抜けた raw INSERT を遮るための DB 側 belt。
        """
        from sqlalchemy import text

        with pytest.raises(IntegrityError):
            await db_session.execute(
                text(
                    "INSERT INTO pending_html_articles "
                    "(url, source_id, status, staged_attributes, "
                    " ready_at, leased_until, attempt_count) "
                    "VALUES (:url, :sid, 'open', '{}'::jsonb, NOW(), NULL, 0)"
                ),
                {
                    "url": "ftp://example.com/x",
                    "sid": sample_source.id,
                },
            )
            await db_session.commit()
