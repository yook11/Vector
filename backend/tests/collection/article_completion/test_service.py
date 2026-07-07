"""``ArticleCompletionService`` の不変条件テスト (PR-E 仕様: ``pending.url`` SSoT)。

検証する不変条件 (DB 状態 = ``analyzable_articles`` / ``incomplete_articles`` の遷移で
振る舞いを assert する。persist 段では ``pipeline_events`` 監査も観測点 — 成功 /
race-loss は状態遷移と同一 tx、真の DB 例外 (経路 9) は別 session で焼かれ再 raise):

- ``execute()`` が成功時 ``int`` (article_id) を返し、失敗・skip・race-loss
  時はすべて ``None`` を返す
- ``incomplete_articles`` の状態遷移が DB に焼き付く
  (成功: DELETE / 永続失敗: closed / 一時失敗 (will retry): open + 未来 ready_at /
  一時失敗 (exhausted): closed)
- 成功時に HTML から抽出した ``body`` / ``title`` / ``published_at`` がそのまま
  ``analyzable_articles`` 行に保存される
- race-loss 時に既存 article は残り、敗者側の pending は DELETE される
- disposition (ScrapeTerminal/ScrapeRetryable) で pending 状態が決まる
  (ScrapeRetryable の BLIP 系 1 回目失敗 = 0.5 分後 / ScrapeTerminal = closed /
  server Retry-After = 指示秒)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_acquisition.strategy import SOURCES
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.scrape_failure import ScrapeNotHtml
from app.collection.article_completion.scraper import ScrapedContent
from app.collection.article_completion.service import ArticleCompletionService
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.external_fetch_errors import (
    FetchGatewayError,
    FetchOriginServerError,
    FetchResourceNotFoundError,
)
from app.collection.sources.article_completion_policy import (
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.source_name import SourceName
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.incomplete_article import IncompleteArticle
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_METRIC = "vector.completion.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


async def _completion_events(db_session: AsyncSession) -> list[PipelineEvent]:
    """service が別 session で commit した completion audit を fresh tx で読む。"""
    await db_session.rollback()
    return list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "completion")
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class _StubArticleSource(BaseArticleSource):
    """``ArticleSource`` Protocol の test 用最小実装。

    ``monkeypatch.setitem(SOURCES, name, _StubArticleSource(...))`` で
    repository の profile lookup を test 単位に差し替える運搬体。
    production registry には登録しない。``read`` / ``map_entry`` は本テストで
    呼ばれないが Protocol shape を満たすため no-op を残す。
    """

    name: SourceName
    completion_policy: ArticleCompletionPolicy
    endpoint_url: str = "https://example.com/feed"
    observed_origin: ObservedOrigin = ObservedOrigin.feed

    async def read(self, tools: ReaderTools) -> list[FetchedArticle]:  # noqa: ARG002
        return []

    def map_entry(self, entry: FetchedArticle) -> FetchedArticle:
        return entry


@pytest.fixture
async def tc_source(db_session: AsyncSession) -> NewsSource:
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


def _observed(
    source: NewsSource,
    url: str,
    *,
    title: str = "TC Title",
    observed_published: PublishedAt | None = PublishedAt(
        datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
    ),
) -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName(str(source.name)),
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value=title, origin=ObservedOrigin.feed),
        published_at=(
            ObservedField(value=observed_published, origin=ObservedOrigin.feed)
            if observed_published is not None
            else None
        ),
    )


async def _load_ready(
    db_session: AsyncSession,
    incomplete_article_id: int,
) -> ReadyForArticleCompletion:
    """Task 層と同じく ``try_advance_from`` で厚い Ready を構築する。

    profile は source registry helper が ``SOURCES[source_name]`` 経由で引く。
    本テストでは production registry の ``tc_source`` 名前一致エントリ
    (TechCrunchSource = DEFAULT_POLICY) を経由する。差し替えたい test は
    ``monkeypatch.setitem`` で SOURCES エントリを上書きしてから呼ぶ。
    """
    ready = await ReadyForArticleCompletion.try_advance_from(
        incomplete_article_id=incomplete_article_id,
        repo=ArticleCompletionRepository(db_session),
    )
    return ready


async def _make_pending(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
    *,
    observed: ObservedArticle | None = None,
) -> tuple[CanonicalArticleUrl, int, ReadyForArticleCompletion]:
    """``incomplete_articles`` 行を 1 件作って claim 状態にし Ready を構築する。

    Returns:
        (canonical_url, incomplete_article_id, ready) — pending は claim 済
        (status='running', attempt_count=1)。``ready`` は Task 層が
        ``try_advance_from`` で構築するのと同じ厚い Ready。
    """
    canonical_url = CanonicalArticleUrl(url)
    incomplete_article_id = await IncompleteArticleRepository(db_session).save(
        observed or _observed(source, url),
        source_id=source.id,
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert incomplete_article_id is not None
    await db_session.commit()
    # claim して running 状態に遷移 (cron poller の代わり)
    now = datetime.now(UTC)
    ids = await ArticleCompletionRepository(db_session).claim_ready_batch(
        limit=10,
        now=now,
        leased_until=now + timedelta(minutes=5),
    )
    await db_session.commit()
    assert incomplete_article_id in ids
    ready = await _load_ready(db_session, incomplete_article_id)
    return canonical_url, incomplete_article_id, ready


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    """``ArticleScraper.scrape`` を Service の import path 経由で差し替える。"""
    monkeypatch.setattr(
        "app.collection.article_completion.service.ArticleScraper.scrape",
        mock,
    )


# 成功 path
# precondition 未充足 (missing / open / sweep 済) で ``None`` を返す経路は Ready
# 構築段の責務になったため、repository (``test_repository.py``) と task
# (``test_scrape_html_body.py``) に移管した。service は厚い Ready だけ受け取る。


@pytest.mark.asyncio
async def test_success_returns_article_id_and_persists_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ScrapedContent + 永続化成功で ``article_id`` を返す。"""
    canonical_url, _, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/article-1"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ArticleCompletionService(session_factory)
    article_id = await svc.execute(ready)

    assert isinstance(article_id, int)
    analyzable_articles = (
        (await db_session.execute(select(AnalyzableArticleRecord))).scalars().all()
    )
    assert len(analyzable_articles) == 1
    assert analyzable_articles[0].id == article_id
    assert str(analyzable_articles[0].source_url) == str(canonical_url)


@pytest.mark.asyncio
async def test_success_deletes_pending_in_same_tx(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功時に pending 行は DELETE される。"""
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/article-2"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ArticleCompletionService(session_factory)
    await svc.execute(ready)

    remaining = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_success_persists_extracted_body_and_published_at(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功時 HTML から抽出した metadata が保存される。

    ``complete_with_html`` が HTML メタデータを ``AnalyzableArticle`` に取り込み、
    ``AnalyzableArticleRepository.save`` が passport 型のまま永続化する不変条件。
    """
    body = "x" * 250
    html_published_at = datetime(2026, 5, 1, 9, 30, 0, tzinfo=UTC)
    # 観測 published=None で HTML published_at を fallback 経路で流入させ、
    # HTML_TITLE_POLICY (title=html_preferred) で HTML title を採用させる。
    # registry helper は ``SOURCES[name].completion_policy`` 経由で解決するため、
    # SOURCES の TechCrunch エントリ (production DEFAULT_POLICY) を test
    # 単位に置き換える。
    monkeypatch.setitem(
        SOURCES,
        tc_source.name,
        _StubArticleSource(name=tc_source.name, completion_policy=HTML_TITLE_POLICY),
    )
    url = "https://techcrunch.com/article-3"
    _, _, ready = await _make_pending(
        db_session,
        tc_source,
        url,
        observed=_observed(tc_source, url, title="Feed Title", observed_published=None),
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body=body,
                published_at=PublishedAt(value=html_published_at),
            )
        ),
    )

    svc = ArticleCompletionService(session_factory)
    article_id = await svc.execute(ready)

    assert isinstance(article_id, int)
    article = (
        await db_session.execute(
            select(AnalyzableArticleRecord).where(
                AnalyzableArticleRecord.id == article_id
            )
        )
    ).scalar_one()
    assert article.original_content == body
    assert article.original_title == "HTML Title"
    assert article.published_at == html_published_at


# ScrapeTerminal disposition (ExternalFetchError terminal / ScrapeFailure / promotion)


@pytest.mark.asyncio
async def test_terminal_fetch_error_returns_none_and_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """terminal な ``ExternalFetchError`` → ``None`` + pending closed。

    404 (``FetchResourceNotFoundError``) は disposition で ``ScrapeTerminal`` に分類
    され、pending は再試行されず ``closed`` に閉じ、record は作成されない。
    """
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/dead"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=FetchResourceNotFoundError(status_code=404, reason="not_found")
        ),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None
    analyzable_articles = (
        (await db_session.execute(select(AnalyzableArticleRecord))).scalars().all()
    )
    assert analyzable_articles == []
    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_scrape_failure_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ScrapeFailure`` → ``None`` + pending status='closed'。"""
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/empty"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(return_value=ScrapeNotHtml(content_type="application/pdf")),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None
    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "closed"


@pytest.mark.asyncio
async def test_promotion_failure_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """promotion ``CompletionRejection`` → ``None`` + pending status='closed'。

    body はあるが published_at が両方 None で promotion failure を発生させる。
    """
    url = "https://techcrunch.com/short"
    _, incomplete_article_id, ready = await _make_pending(
        db_session,
        tc_source,
        url,
        observed=_observed(
            tc_source, url, title="Short Title", observed_published=None
        ),
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(title="OK", body="x" * 200, published_at=None)
        ),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None
    analyzable_articles = (
        (await db_session.execute(select(AnalyzableArticleRecord))).scalars().all()
    )
    assert analyzable_articles == []
    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "closed"


# ScrapeRetryable disposition → will_retry / exhausted


@pytest.mark.asyncio
async def test_temporary_blip_first_attempt_writes_will_retry(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLIP 1 回目失敗 → ``None`` + pending re-open + 未来 ready_at (0.5 分後)。

    502 (``FetchGatewayError``) は disposition で BLIP schedule の ``ScrapeRetryable``。
    delay schedule[0] = 0.5 分なので next ready_at は約 30 秒後。
    """
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/blip"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(return_value=FetchGatewayError(status_code=502)),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None

    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "open"
    assert pending.leased_until is None
    # BLIP 1 回目: 0.5 分後 (= 30 秒後)
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=20) < delta < timedelta(seconds=40)


@pytest.mark.asyncio
async def test_temporary_outage_exhausted_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempt_count == max_attempts → ``None`` + pending status='closed'。

    503 (Retry-After なし) は disposition で OUTAGE schedule の ``ScrapeRetryable``。
    OUTAGE.max_attempts = 12 に到達済なので exhausted で ``closed``。
    """
    _, incomplete_article_id, _ = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/outage"
    )
    # OUTAGE.max_attempts = 12 を超過させる: attempt_count を 12 に強制セット
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == incomplete_article_id)
        .values(attempt_count=12)
    )
    await db_session.commit()
    # attempt_count 更新後の状態で Ready を再構築 (exhausted 判定の SSoT)
    ready = await _load_ready(db_session, incomplete_article_id)
    assert ready.attempt_count == 12
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=FetchOriginServerError(
                status_code=503,
                reason="service_unavailable",
                retry_after_seconds=None,
            )
        ),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None

    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_temporary_retry_after_uses_server_delay(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 + Retry-After → ``None`` + pending re-open + server 指示秒の ready_at。

    ``FetchOriginServerError(service_unavailable, retry_after_seconds=120)`` は
    disposition で OUTAGE schedule + server 指示の ``FixedDelay`` を持つ
    ``ScrapeRetryable``。120 秒 → 2 分に換算して next ready_at にする。
    """
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/retry-after"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=FetchOriginServerError(
                status_code=503,
                reason="service_unavailable",
                retry_after_seconds=120.0,
            )
        ),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None

    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "open"
    assert pending.leased_until is None
    # server 指示 120 秒 = 2 分後
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=100) < delta < timedelta(seconds=140)


# race-loss (永続化層 → pending delete、敗者 article は INSERT しない)


@pytest.mark.asyncio
async def test_race_lost_returns_none_and_deletes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 worker が article を先に作った → ``None`` + pending DELETE + 既存 article 残置.

    pre-condition: 同 ``source_url`` の AnalyzableArticleRecord を直接 INSERT (race の "勝者")。
    """  # noqa: E501
    canonical_url, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/race"
    )
    # winner 役の AnalyzableArticleRecord を先に INSERT (同一 canonical source_url)
    existing = AnalyzableArticleRecord(
        original_title="Existing",
        original_content="y" * 100,
        published_at=datetime(2026, 4, 30, tzinfo=UTC),
        source_id=tc_source.id,
        source_url=canonical_url,
    )
    db_session.add(existing)
    await db_session.commit()

    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="z" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ArticleCompletionService(session_factory)
    outcome = await svc.execute(ready)

    assert outcome is None
    # analyzable_articles は 1 件のまま (敗者は INSERT しない)
    analyzable_articles = (
        (await db_session.execute(select(AnalyzableArticleRecord))).scalars().all()
    )
    assert len(analyzable_articles) == 1
    # pending は DELETE
    remaining = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_superseded_attempt_returns_none_and_keeps_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 worker が再 claim し attempt_count がズレた → ``None`` + article 0 件 + pending 残置.

    fence DELETE は ``incomplete_article_id`` + ``attempt_count`` で gate される。別 worker が
    再 claim して DB の世代が ready の握る値と食い違うと DELETE は 0 行になり、
    article INSERT は実行されず pending 行も残る (UrlConflict が pending を DELETE
    するのと対になる差分)。
    """  # noqa: E501
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/stale"
    )
    # 別 worker の再 claim を模す: DB の attempt_count を ready が握る値からズラす
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == incomplete_article_id)
        .values(attempt_count=ready.attempt_count + 1)
    )
    await db_session.commit()
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    outcome = await ArticleCompletionService(session_factory).execute(ready)

    assert outcome is None
    # 失効 worker は INSERT しない
    assert (
        await db_session.execute(select(AnalyzableArticleRecord))
    ).scalars().all() == []
    # DELETE は attempt 不一致で 0 行 → pending は残る (UrlConflict との差分)
    remaining = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one_or_none()
    assert remaining is not None


# persist 段 audit 配線 (route 1 / 6 / 7 = same-tx、route 9 = 別 session + re-raise)


@pytest.mark.asyncio
async def test_success_writes_article_completed_audit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 → ``succeeded`` / ``article_completed`` audit を INSERT と同 tx。"""
    _, _, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/audit-ok"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    article_id = await ArticleCompletionService(session_factory).execute(ready)

    events = await _completion_events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "succeeded"
    assert events[0].outcome_code == "article_completed"
    assert events[0].retryability is None
    assert events[0].article_id == article_id
    assert events[0].payload["attempt_count"] == ready.attempt_count


@pytest.mark.asyncio
async def test_url_conflict_writes_no_audit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 worker が同 URL を先に記事化 (race-loss) → 監査に焼かない (log で観測)。"""
    canonical_url, _, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/audit-conflict"
    )
    db_session.add(
        AnalyzableArticleRecord(
            original_title="Existing",
            original_content="y" * 100,
            published_at=datetime(2026, 4, 30, tzinfo=UTC),
            source_id=tc_source.id,
            source_url=canonical_url,
        )
    )
    await db_session.commit()
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="z" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    with capture_logs() as logs:
        await ArticleCompletionService(session_factory).execute(ready)

    events = await _completion_events(db_session)
    assert events == []
    # 監査を外した代わりに race-loss は escape log で観測可能に保つ。
    assert [e for e in logs if e.get("event") == "article_completion_conflict_lost"]


@pytest.mark.asyncio
async def test_superseded_writes_no_audit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempt 失効で DELETE 0 行 (race-loss) → 監査に焼かない (log で観測)。"""
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/audit-superseded"
    )
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == incomplete_article_id)
        .values(attempt_count=ready.attempt_count + 1)
    )
    await db_session.commit()
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    with capture_logs() as logs:
        await ArticleCompletionService(session_factory).execute(ready)

    events = await _completion_events(db_session)
    assert events == []
    # 監査を外した代わりに race-loss は escape log で観測可能に保つ。
    assert [
        e for e in logs if e.get("event") == "article_completion_stale_attempt_ignored"
    ]


@pytest.mark.asyncio
async def test_persist_db_exception_writes_persist_crashed_and_reraises(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """persist の真の DB 例外 (経路 9) → 別 session で ``persist_crashed`` + 再 raise。

    同一 tx audit (経路 1/6/7) は rollback に巻き込まれるため、本経路だけは別 session
    で焼かれ痕跡が残る。pending は running のまま (lease 失効 → sweep で self-heal)。
    """
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/audit-crash"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    async def _boom(self: object, ready: object, advanced: object) -> None:  # noqa: ARG001
        raise RuntimeError("db connection lost mid-INSERT")

    monkeypatch.setattr(
        "app.collection.article_completion.service."
        "ArticleCompletionRepository.persist_completed",
        _boom,
    )

    svc = ArticleCompletionService(session_factory)
    with pytest.raises(RuntimeError, match="db connection lost"):
        await svc.execute(ready)

    events = await _completion_events(db_session)
    assert len(events) == 1
    assert events[0].event_type == "failed"
    assert events[0].outcome_code == "persist_crashed"
    assert events[0].retryability == "unknown"
    assert events[0].error_class.endswith(".RuntimeError")
    assert events[0].payload["attempt_count"] == ready.attempt_count
    assert events[0].payload["failure_kind"] == "persist_crashed"
    assert events[0].payload["failure_action"] is None
    # 状態は触られず running のまま (self-heal は lease 失効に委ねる)
    pending = (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()
    assert pending.status == "running"


# processing_outcome metric (vector.completion.processing_outcome)


@pytest.mark.asyncio
async def test_success_emits_processing_outcome_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """保存 + audit commit 後に processing_outcome{result=succeeded} が +1。"""
    _, _, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/metric-ok"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    await ArticleCompletionService(session_factory).execute(ready)

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, "succeeded") == 1
    for other in ("failed", "infra_error"):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


@pytest.mark.asyncio
async def test_superseded_does_not_emit_processing_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """claim 喪失 (attempt 失効) の persist superseded は counter を汚さない。"""
    _, incomplete_article_id, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/metric-superseded"
    )
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == incomplete_article_id)
        .values(attempt_count=ready.attempt_count + 1)
    )
    await db_session.commit()
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    await ArticleCompletionService(session_factory).execute(ready)

    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


@pytest.mark.asyncio
async def test_url_conflict_does_not_emit_processing_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """同一 URL 衝突 (CompletionUrlConflict) は dedup の揺れで counter を汚さない。"""
    canonical_url, _, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/metric-conflict"
    )
    db_session.add(
        AnalyzableArticleRecord(
            original_title="Existing",
            original_content="y" * 100,
            published_at=datetime(2026, 4, 30, tzinfo=UTC),
            source_id=tc_source.id,
            source_url=canonical_url,
        )
    )
    await db_session.commit()
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="z" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    await ArticleCompletionService(session_factory).execute(ready)

    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


@pytest.mark.asyncio
async def test_persist_db_crash_emits_infra_error_not_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """persist の DB 例外 → infra_error、succeeded は emit しない。

    succeeded emit が persist commit 境界の後ろにある不変条件の回帰ガード。
    handle_persist_crashed が SQLAlchemyError を infra に分類する。
    """
    _, _, ready = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/metric-crash"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ScrapedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    async def _boom(self: object, ready: object, advanced: object) -> None:  # noqa: ARG001
        raise SQLAlchemyError("db connection lost mid-INSERT")

    monkeypatch.setattr(
        "app.collection.article_completion.service."
        "ArticleCompletionRepository.persist_completed",
        _boom,
    )

    with pytest.raises(SQLAlchemyError):
        await ArticleCompletionService(session_factory).execute(ready)

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, "infra_error") == 1
    for other in ("succeeded", "failed"):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0
