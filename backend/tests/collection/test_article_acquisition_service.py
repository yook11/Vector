"""``ArticleAcquisitionService`` の取得・変換・保存・監査テスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition import service as service_module
from app.collection.article_acquisition.errors import (
    AcquisitionExternalFetchError,
    AcquisitionUnreadableResponseError,
    UnreadableResponseError,
)
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.service import ArticleAcquisitionService
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
)
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.source_name import SourceName
from app.models.article import Article as ArticleORM
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent

_PUBLISHED = datetime(2026, 4, 30, tzinfo=UTC)


def _ready_fetched(url: str) -> FetchedArticle:
    """real convert → ``AnalyzableArticle`` (body + published 揃い, DEFAULT_POLICY)。"""
    return FetchedArticle(
        title="Test Title", url=url, body="x" * 100, published_at=_PUBLISHED
    )


def _pending_fetched(url: str) -> FetchedArticle:
    """real convert → ``ObservedArticle`` (body=None で Ready 不成立)。"""
    return FetchedArticle(title="TC Title", url=url, body=None, published_at=_PUBLISHED)


def _rejection_fetched(url: str = "https://venturebeat.com/x") -> FetchedArticle:
    """real convert → ``MISSING_TITLE`` の ``ConversionRejection``。

    title は whitespace: ``bool(title)`` は True (= ``has_title`` True) だが
    strip 後は空で MISSING_TITLE 棄却になる。body 42 / published None で
    audit payload の構造化 field を固定する。
    """
    return FetchedArticle(title="   ", url=url, body="x" * 42, published_at=None)


def _url_rejection_fetched(url: str = "http://127.0.0.1/secret") -> FetchedArticle:
    """real convert → URL VO 失敗 (private IP) の ``ConversionRejection``。

    title は揃うが SafeUrl の SSRF 防御で ``host_not_public_ip`` 棄却になる。
    ``CanonicalArticleUrlInvalidError`` → ``SafeUrlInvalidError`` の cause 連鎖を
    監査が辿れる (error_chain 深さ>1) ことを固定するための入力。
    """
    return FetchedArticle(
        title="Has Title", url=url, body="x" * 42, published_at=_PUBLISHED
    )


def _bug_fetched(url: str) -> FetchedArticle:
    """convert を monkeypatch で raise させる対象 entry (URL で識別)。"""
    return FetchedArticle(
        title="Bug Title", url=url, body="x" * 100, published_at=_PUBLISHED
    )


class _StubSource(BaseArticleSource):
    """``FetchedArticle`` を直接注入する ``ArticleSource`` 構造的 fake。

    identity / 補完方針は本物の RSS source 相当 (feed + DEFAULT_POLICY) に
    固定し、``read`` は注入された ``FetchedArticle`` 列をそのまま返す
    (Entry 型 = ``FetchedArticle``、``map_entry`` は恒等、in_scope/select は
    ``BaseArticleSource`` の default)。
    """

    name: ClassVar[SourceName] = SourceName("VentureBeat")
    endpoint_url: ClassVar[str] = "https://venturebeat.com/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    def __init__(self, items: list[FetchedArticle]) -> None:
        self._items = items

    async def read(
        self,
        tools: ReaderTools,  # noqa: ARG002
    ) -> list[FetchedArticle]:
        return self._items

    def map_entry(self, entry: FetchedArticle) -> FetchedArticle:
        return entry


class _RaisingReadSource(BaseArticleSource):
    """``read`` で取得 / 読取 origin error を発生させる fake source。"""

    name: ClassVar[SourceName] = SourceName("VentureBeat")
    endpoint_url: ClassVar[str] = "https://venturebeat.com/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY

    def __init__(self, exc: ExternalFetchError | UnreadableResponseError) -> None:
        self._exc = exc

    async def read(
        self,
        tools: ReaderTools,  # noqa: ARG002
    ) -> list[FetchedArticle]:
        raise self._exc

    def map_entry(self, entry: FetchedArticle) -> FetchedArticle:
        return entry


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
        _StubSource([_ready_fetched("https://venturebeat.com/a/")]),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    assert isinstance(article_ids[0], int)

    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
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
    """補完待ち獲得経路は incomplete_articles を作り、url は canonicalize 済み値。"""
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_pending_fetched("https://techcrunch.com/h/")]),
    )

    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []  # 補完待ち経路は cron poller 駆動

    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
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
        _StubSource([_pending_fetched("https://techcrunch.com/known")]),
    )
    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
    assert pendings == []  # pre-check で弾かれて pending を作っていない


@pytest.mark.asyncio
async def test_empty_yield_does_not_persist(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """Source が 1 件も yield しないとき、永続化は走らない。"""
    svc = ArticleAcquisitionService(session_factory, _StubSource([]))

    article_ids = await svc.execute(vb_source.id)

    assert article_ids == []
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
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
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/dup/"),
                _ready_fetched("https://venturebeat.com/dup/"),
            ]
        ),
    )

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
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/a"),
                _ready_fetched("https://venturebeat.com/a/?utm_source=twitter"),
            ]
        ),
    )

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
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/ok/"),
                _pending_fetched("https://techcrunch.com/h/"),
            ]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
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
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/ok/"),
                _rejection_fetched(),
                _pending_fetched("https://techcrunch.com/h/"),
            ]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1  # 棄却を挟んでも R は永続化
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
    assert len(articles) == 1
    assert len(pendings) == 1  # 棄却後の H も止まらず投入


@pytest.mark.asyncio
async def test_conversion_rejection_writes_rejected_event_in_separate_tx(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """棄却監査は別 session に commit 済の REJECTED 行として残る。

    ``stage='acquisition'`` / ``event_type='rejected'`` 固定、``outcome_code`` は
    責任元の reason を verbatim で運ぶ (title 欠落は acquisition 所有の
    ``acquisition_conversion_title_missing``)。観測スナップショットは
    ``payload.conversion_*`` 構造化列で SQL drill-down できる。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_rejection_fetched()]),
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
    assert row.stage == "acquisition"
    assert row.outcome_code == "acquisition_conversion_title_missing"
    assert row.retryability is None
    assert row.source_id == vb_source.id
    # title 欠落は責任元 VO 例外を持たない (acquisition 方針違反)。
    # cause 無し → error_class は NULL。
    assert row.error_class is None
    assert row.payload["conversion_has_title"] is True
    assert row.payload["conversion_body_length"] == 42
    assert row.payload["conversion_has_published_at"] is False


@pytest.mark.asyncio
async def test_invalid_url_rejection_burns_url_vo_reason_with_cause_chain(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """URL VO 失敗は責任元 reason を verbatim で焼き、cause 連鎖を監査に残す。

    private IP は SafeUrl の SSRF 防御で ``host_not_public_ip`` に精密分類され
    (旧 ``INVALID_URL`` 潰しを解消)、``CanonicalArticleUrlInvalidError`` →
    ``SafeUrlInvalidError`` の cause が ``error_class`` / ``error_chain`` (深さ>1)
    として残る。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_url_rejection_fetched()]),
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
    assert row.outcome_code == "host_not_public_ip"
    assert row.error_class.endswith(".CanonicalArticleUrlInvalidError")
    # URL VO 失敗は下位 SafeUrl 失敗を __cause__ に連鎖 → chain 深さ>1 (非空虚)。
    assert row.payload["error_chain"] is not None
    assert len(row.payload["error_chain"]) > 1


@pytest.mark.asyncio
async def test_unexpected_convert_bug_is_valued_and_stream_continues(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """convert が想定外 bug を raise しても service が値化し source を止めない。

    bug entry は握りつぶされず ``UNEXPECTED_ERROR`` として別 tx で監査され
    (``payload`` を SQL drill-down できる)、前後の R / H は通常どおり永続化
    される (1 件の bug で source 全体が落ちない = stream resilience は
    orchestrator の責務)。
    """
    real_convert = service_module.convert_fetched_article

    def _flaky_convert(fetched: FetchedArticle, *, source: object, source_id: int):  # noqa: ANN202
        if fetched.url == "https://venturebeat.com/bug":
            raise RuntimeError("post-precondition invariant violation")
        return real_convert(fetched, source=source, source_id=source_id)

    monkeypatch.setattr(service_module, "convert_fetched_article", _flaky_convert)

    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/ok/"),
                _bug_fetched("https://venturebeat.com/bug"),
                _pending_fetched("https://techcrunch.com/h/"),
            ]
        ),
    )

    article_ids = await svc.execute(vb_source.id)

    assert len(article_ids) == 1  # bug を挟んでも R は永続化
    pendings = (await db_session.execute(select(IncompleteArticleORM))).scalars().all()
    assert len(pendings) == 1  # bug 後の H も止まらず投入

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.outcome_code
                    == "acquisition_conversion_unexpected_error"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # error_class は本当のバグ (cause) 由来 — 再分類器でラップしない。
    assert rows[0].error_class.endswith(".RuntimeError")


async def _succeeded_events(db_session: AsyncSession) -> list[PipelineEvent]:
    return list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "succeeded")
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_external_fetch_error_is_wrapped_to_acquisition_marker(
    session_factory: async_sessionmaker[AsyncSession],
    vb_source: NewsSource,
) -> None:
    """外部取得 origin error は Stage 1 terminal marker に詰め替えて伝播する。"""
    origin = FetchAccessDeniedError(status_code=403, reason="forbidden")
    svc = ArticleAcquisitionService(session_factory, _RaisingReadSource(origin))

    with pytest.raises(AcquisitionExternalFetchError) as raised:
        await svc.execute(vb_source.id)

    assert raised.value.code == "fetch_access_denied"
    assert raised.value.origin_error is origin
    assert raised.value.__cause__ is origin


@pytest.mark.asyncio
async def test_unreadable_response_error_is_wrapped_to_acquisition_marker(
    session_factory: async_sessionmaker[AsyncSession],
    vb_source: NewsSource,
) -> None:
    """読取 origin error は Stage 1 unreadable marker に詰め替えて伝播する。"""
    origin = UnreadableResponseError("rss bozo")
    svc = ArticleAcquisitionService(session_factory, _RaisingReadSource(origin))

    with pytest.raises(AcquisitionUnreadableResponseError) as raised:
        await svc.execute(vb_source.id)

    assert raised.value.code == "read_unreadable_response"
    assert raised.value.origin_error is origin
    assert raised.value.__cause__ is origin


@pytest.mark.asyncio
async def test_immediate_acquisition_writes_article_created_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """即時獲得成功は SUCCEEDED/article_created を同一 tx で 1 行焼く。

    ``article_id`` は採番済み新規行 (execute 戻り値と一致)、``canonical_url`` は
    canonicalize 済み値、``retryability`` / ``error_class`` は collection 成功なので
    NULL。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_ready_fetched("https://venturebeat.com/a/")]),
    )

    article_ids = await svc.execute(vb_source.id)

    rows = await _succeeded_events(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.stage == "acquisition"
    assert row.outcome_code == "article_created"
    assert row.article_id == article_ids[0]  # 採番済み新規行 id
    assert row.source_id == vb_source.id
    assert row.retryability is None
    assert row.error_class is None
    # canonicalize で trailing slash 削除済
    assert row.payload["canonical_url"] == "https://venturebeat.com/a"


@pytest.mark.asyncio
async def test_incomplete_staging_writes_incomplete_article_created_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """補完待ち投入成功は SUCCEEDED/incomplete_article_created を焼く。

    ``article_id`` はこの段ではまだ無い (後段 completion の promote 時に採番)。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_pending_fetched("https://techcrunch.com/h/")]),
    )

    article_ids = await svc.execute(vb_source.id)
    assert article_ids == []  # 補完待ち経路は cron poller 駆動

    rows = await _succeeded_events(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome_code == "incomplete_article_created"
    assert row.article_id is None  # 補完後の promote 時に採番
    assert row.retryability is None
    assert row.payload["canonical_url"] == "https://techcrunch.com/h"


@pytest.mark.asyncio
async def test_redelivered_full_content_writes_single_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """同 URL 再掲は ON CONFLICT で skip され、SUCCEEDED は初回 1 件のみ (bounded)。

    成功 witness は新規 URL 初回のみ発火し、定常的な重複再掲は非記録。2 件 yield して
    1 件しか焼かれないことで「件数ぶん焼く」naive 実装と区別する (非空虚)。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/dup/"),
                _ready_fetched("https://venturebeat.com/dup/"),
            ]
        ),
    )

    await svc.execute(vb_source.id)

    rows = await _succeeded_events(db_session)
    assert len(rows) == 1  # 2 度目は ON CONFLICT → 非記録
    assert rows[0].outcome_code == "article_created"


@pytest.mark.asyncio
async def test_known_url_observed_writes_no_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
) -> None:
    """既知 URL を補完待ち経路で受けると pre-check skip され SUCCEEDED を焼かない。"""
    canonical = CanonicalArticleUrl("https://techcrunch.com/known")
    db_session.add(
        ArticleORM(
            original_title="Already there",
            original_content="x" * 100,
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
            source_id=vb_source.id,
            source_url=canonical,
        )
    )
    await db_session.commit()

    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_pending_fetched("https://techcrunch.com/known")]),
    )
    await svc.execute(vb_source.id)

    assert await _succeeded_events(db_session) == []  # pre-check skip は非記録
