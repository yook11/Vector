"""``PendingHtmlArticle`` ORM の不変条件テスト (CHECK / UNIQUE / FK)。

state model の整合性 (status × leased_until の組合せ / ready_at 必須等) が
DB レベルで強制されていることを実 PG fixture で確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArticleUrl, NewsSource, PendingHtmlArticle
from app.shared.value_objects.safe_url import SafeUrl


@pytest.fixture
async def sample_url(db_session: AsyncSession, sample_source: NewsSource) -> ArticleUrl:
    """テスト用 article_urls 行 1 件を作る。"""
    url = ArticleUrl(
        normalized_url=SafeUrl("https://example.com/pending"),
        original_url=SafeUrl("https://example.com/pending"),
        first_seen_source_id=sample_source.id,
    )
    db_session.add(url)
    await db_session.commit()
    await db_session.refresh(url)
    return url


def _now() -> datetime:
    return datetime.now(UTC)


class TestPendingHtmlArticleHappyPath:
    @pytest.mark.asyncio
    async def test_open_with_ready_at_persists(
        self,
        db_session: AsyncSession,
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # 通常の open 行: ready_at 必須、leased_until は NULL
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # claim 直後の running: leased_until が値を持ち、ready_at も値を持つ
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # closed は再試行しないので ready_at NULL でも OK
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # open なのに leased_until が残っている = state 不整合
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # running なのに leased_until NULL = claim 整合性違反
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # open なのに ready_at NULL だと picking から永久に漏れる事故
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        pending = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
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
    async def test_article_url_id_is_unique(
        self,
        db_session: AsyncSession,
        sample_url: ArticleUrl,
        sample_source: NewsSource,
    ) -> None:
        # 同一 article_url_id に対し pending 行は最大 1 つ (cross-table dedup の片肺)
        first = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=sample_url.normalized_url,
            source_id=sample_source.id,
            status="open",
            staged_attributes={},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(first)
        await db_session.commit()

        # url は別値にして ``UNIQUE(article_url_id)`` 違反だけを誘発する
        duplicate = PendingHtmlArticle(
            article_url_id=sample_url.id,
            url=SafeUrl("https://example.com/pending-dup"),
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
    async def test_url_is_unique(
        self,
        db_session: AsyncSession,
        sample_source: NewsSource,
    ) -> None:
        """PR-D: ``url`` 列にも UNIQUE が張られている (cross-table dedup の支柱)。"""
        url_a = ArticleUrl(
            normalized_url=SafeUrl("https://example.com/a"),
            original_url=SafeUrl("https://example.com/a"),
            first_seen_source_id=sample_source.id,
        )
        url_b = ArticleUrl(
            normalized_url=SafeUrl("https://example.com/b"),
            original_url=SafeUrl("https://example.com/b"),
            first_seen_source_id=sample_source.id,
        )
        db_session.add_all([url_a, url_b])
        await db_session.commit()
        await db_session.refresh(url_a)
        await db_session.refresh(url_b)

        first = PendingHtmlArticle(
            article_url_id=url_a.id,
            url=SafeUrl("https://example.com/shared"),
            source_id=sample_source.id,
            status="open",
            staged_attributes={},
            ready_at=_now(),
            leased_until=None,
        )
        db_session.add(first)
        await db_session.commit()

        # article_url_id は別、url のみ衝突 → UNIQUE(url) 違反
        duplicate = PendingHtmlArticle(
            article_url_id=url_b.id,
            url=SafeUrl("https://example.com/shared"),
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
        sample_url: ArticleUrl,
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
                    "(article_url_id, url, source_id, status, staged_attributes, "
                    " ready_at, leased_until, attempt_count) "
                    "VALUES (:auid, :url, :sid, 'open', '{}'::jsonb, NOW(), NULL, 0)"
                ),
                {
                    "auid": sample_url.id,
                    "url": "ftp://example.com/x",
                    "sid": sample_source.id,
                },
            )
            await db_session.commit()
