"""``ArticleAcquisitionService`` の振り分け責務テスト。

PR-E 以降は新 2 表 (``articles`` / ``pending_html_articles``) を直接駆動する。

検証する不変条件:

- 即時獲得経路 (``AnalyzableArticle``): ``articles.source_url``
  (型 ``CanonicalArticleUrl`` で canonicalize 済が構造保証) に直 INSERT、
  ``execute()`` 戻り値の ``list[int]`` に永続化された article_id が積まれる
- 補完待ち獲得経路 (``IncompleteArticle``): ``seen_repo.exists_by_source_url``
  pre-check を通過したら ``pending_html_articles.url`` で INSERT。Outcome は
  純化されているため caller には何も渡らない (cron poller が DB 駆動)
- 同 URL の重複 yield は ``articles.source_url UNIQUE`` で 1 件に絞られる
- ``CanonicalArticleUrl`` 型構築時点で tracking parameter / trailing slash が
  吸収される (Service 側で後付け正規化を行わない)
- 既知 URL (= articles 既存) を補完待ち経路で受けたら pre-check で skip
- 混在 (即時 + 補完待ち) でも各経路が独立して正しく分岐する

PR-2 (Outcome 純化): 品質ゲート未達は Fetcher 側で yield しないため、
Service の責務は「渡された passport は次工程に進めるべきもの」前提のみ。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.errors import (
    ConversionReason,
    FetchedArticleConversionError,
)
from app.collection.source_fetch.fetched_article_converter import ConversionRejection
from app.collection.source_fetch.service import ArticleAcquisitionService
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
from app.models.pipeline_event import PipelineEvent
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName

Passport = AnalyzableArticle | ObservedArticle
FetchItem = AnalyzableArticle | ObservedArticle | ConversionRejection


def _ready(source_id: int, url: str) -> AnalyzableArticle:
    return AnalyzableArticle(
        title="Test Title",
        body="x" * 100,
        published_at=PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC)),
        source_id=source_id,
        source_url=CanonicalArticleUrl(url),
    )


def _pending(source_name: SourceName, url: str) -> ObservedArticle:
    return ObservedArticle(
        source_name=source_name,
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value="TC Title", origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(value=datetime(2026, 4, 30, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )


def _rejection(
    *,
    analyzable_reason: ConversionReason = ConversionReason.BODY_TOO_SHORT,
    observed_reason: ConversionReason = ConversionReason.MISSING_TITLE,
) -> ConversionRejection:
    return ConversionRejection(
        error=FetchedArticleConversionError(
            f"analyzable rejected: {analyzable_reason}; "
            f"observed rejected: {observed_reason}",
            analyzable_reason=analyzable_reason,
            observed_reason=observed_reason,
            source_name="VentureBeat",
            raw_url="https://venturebeat.com/x",
            has_title=True,
            body_length=42,
            has_published_at=False,
        )
    )


class _StubFetcher:
    def __init__(self, items: list[FetchItem]) -> None:
        self._items = items

    async def fetch(self, source_id: int) -> AsyncIterator[FetchItem]:
        for item in self._items:
            yield item


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


@pytest.mark.asyncio
async def test_pattern_r_inserts_canonicalized_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """即時獲得経路は articles を 1 件作り、source_url が canonicalize 済み値で入る。"""
    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher([_ready(vb_source.id, "https://venturebeat.com/a/")]),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    assert isinstance(article_ids[0], int)

    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(articles) == 1
    # canonicalize で trailing slash 削除済
    assert str(articles[0].source_url) == "https://venturebeat.com/a"
    assert pendings == []


@pytest.mark.asyncio
async def test_pattern_h_inserts_pending_with_canonicalized_url(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """補完待ち獲得経路は pending_html_articles を作り、url は canonicalize 済み値。"""
    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher([_pending(vb_source.name, "https://techcrunch.com/h/")]),
    )

    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []  # 補完待ち経路は cron poller 駆動

    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert articles == []
    assert len(pendings) == 1
    assert str(pendings[0].url) == "https://techcrunch.com/h"
    assert pendings[0].status == "open"
    assert pendings[0].attempt_count == 0


@pytest.mark.asyncio
async def test_pattern_h_skips_when_article_already_exists(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """補完待ち経路 pre-check: articles に同 URL がある場合は pending を作らず skip。

    feed 再露出時の HTML fetch 反復を抑える実用的 idempotency の検証。
    """
    canonical = CanonicalArticleUrl("https://techcrunch.com/known")
    existing = ArticleORM(
        original_title="Already there",
        original_content="x" * 100,
        published_at=datetime(2026, 4, 1, tzinfo=UTC),
        source_id=vb_source.id,
        source_url=canonical,
    )
    db_session.add(existing)
    await db_session.commit()

    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher(
            [_pending(vb_source.name, "https://techcrunch.com/known")]
        ),
    )
    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert pendings == []  # pre-check で弾かれて pending を作っていない


@pytest.mark.asyncio
async def test_empty_yield_does_not_persist(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Fetcher が 1 件も yield しないとき、永続化は走らない。

    Outcome 純化原則: 品質ゲート未達 entry は Fetcher 側で drop されるため、
    Service には届かない。
    """
    svc = ArticleAcquisitionService(session_factory, lambda: _StubFetcher([]))

    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert articles == []
    assert pendings == []


@pytest.mark.asyncio
async def test_duplicate_url_yielded_twice_persists_once(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """同 URL の重複 yield は ``articles.source_url UNIQUE`` で 1 件に絞られる。

    2 度目は ON CONFLICT DO NOTHING で ``known_url`` skip となる。
    """
    e1 = _ready(vb_source.id, "https://venturebeat.com/dup/")
    e2 = _ready(vb_source.id, "https://venturebeat.com/dup/")
    svc = ArticleAcquisitionService(session_factory, lambda: _StubFetcher([e1, e2]))

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_canonicalization_dedupes_tracking_query(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """canonicalize_url が tracking parameter / trailing slash を吸収する。

    異なる原始 URL でも canonicalize 後が同じなら ``articles.source_url UNIQUE``
    で 2 度目は弾かれ ``known_url`` skip。
    """
    e1 = _ready(vb_source.id, "https://venturebeat.com/a")
    e2 = _ready(vb_source.id, "https://venturebeat.com/a/?utm_source=twitter")
    svc = ArticleAcquisitionService(session_factory, lambda: _StubFetcher([e1, e2]))

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1


@pytest.mark.asyncio
async def test_mixed_ready_pending_route_independently(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """混在 (R + H) でも各経路が独立して正しく分岐する。"""
    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher(
            [
                _ready(vb_source.id, "https://venturebeat.com/ok/"),
                _pending(vb_source.name, "https://techcrunch.com/h/"),
            ]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(articles) == 1  # R only
    assert len(pendings) == 1  # H only


@pytest.mark.asyncio
async def test_conversion_rejection_audited_without_stopping_source(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """棄却を挟んでも他 entry は永続化され source は止まらない。

    棄却は握りつぶさず別 tx で ``pipeline_events`` に焼かれ、後続の R / H は
    通常どおり永続化される (1 件不良で source 全体が落ちない)。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher(
            [
                _ready(vb_source.id, "https://venturebeat.com/ok/"),
                _rejection(),
                _pending(vb_source.name, "https://techcrunch.com/h/"),
            ]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1  # 棄却を挟んでも R は永続化
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(PendingHtmlArticleORM))).scalars().all()
    assert len(articles) == 1
    assert len(pendings) == 1  # 棄却後の H も止まらず投入


@pytest.mark.asyncio
async def test_conversion_rejection_writes_rejected_event_in_separate_tx(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """棄却監査は別 session に commit 済の REJECTED 行として残る。

    ``stage='source_fetch'`` / ``event_type='rejected'`` 固定、``code`` /
    ``outcome_code`` は単一 code、``category`` は collection stage なので NULL。
    深刻度細分は ``payload.conversion_*`` 構造化列で SQL drill-down できる。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher([_rejection()]),
    )

    await svc.execute(vb_source.id)

    row = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "rejected")
            )
        )
        .scalars()
        .one()
    )
    assert row.stage == "source_fetch"
    assert row.code == "fetched_article_conversion_failed"
    assert row.outcome_code == "fetched_article_conversion_failed"
    assert row.category is None
    assert row.source_id == vb_source.id
    assert row.attempt == 1
    assert row.error_class.endswith(".FetchedArticleConversionError")
    assert row.payload["conversion_analyzable_reason"] == "body_too_short"
    assert row.payload["conversion_observed_reason"] == "missing_title"
    assert row.payload["conversion_has_title"] is True
    assert row.payload["conversion_body_length"] == 42
    assert row.payload["conversion_has_published_at"] is False


@pytest.mark.asyncio
async def test_conversion_rejection_payload_is_sql_drillable(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """``payload->>'conversion_analyzable_reason'`` で JSONB drill-down できる。"""
    svc = ArticleAcquisitionService(
        session_factory,
        lambda: _StubFetcher(
            [_rejection(analyzable_reason=ConversionReason.READY_PRECLUDED)]
        ),
    )

    await svc.execute(vb_source.id)

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.payload["conversion_analyzable_reason"].astext
                    == "ready_precluded"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
