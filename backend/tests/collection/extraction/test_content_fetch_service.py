"""``ContentFetchService`` の不変条件テスト (PR-E 仕様: ``pending.url`` SSoT)。

検証する不変条件:

- ``execute()`` が成功時 ``int`` (article_id) を返し、失敗・skip・race-loss
  時はすべて ``None`` を返す。失敗詳細は ``pipeline_events`` の
  ``outcome_code`` (``fetched`` / ``conflict_lost`` / ``dropped_terminal`` /
  ``dropped_transient`` / ``will_retry``) と ``payload.reason_code`` で観測
- ``pending_html_articles`` の状態遷移が DB に焼き付く
  (成功: DELETE / 永続失敗: closed / 一時失敗 (will retry): open + 未来 ready_at /
  一時失敗 (exhausted): closed)
- ``canonical_url`` が pipeline_events.payload に焼かれる (集計 key)
- 重複配送 / 状態不整合 (status != 'running') は ``None`` で静かに exit
- per-error retry policy で next ready_at が決まる (BLIP の 1 回目失敗 = 0.5 分後)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.article.domain.value_objects import PublishedAt
from app.collection.errors import (
    PermanentFetchError,
    ServerErrorBlip,
    ServerErrorOutage,
)
from app.collection.extraction.content_fetch_service import ContentFetchService
from app.collection.extraction.extractor import ExtractedContent, ExtractionEmpty
from app.collection.incomplete_article.domain.staged_attributes import (
    StagedArticleAttributes,
)
from app.collection.incomplete_article.repository import (
    PendingHtmlArticleRepository,
)
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pending_html_article import PendingHtmlArticle
from app.models.pipeline_event import PipelineEvent
from app.shared.value_objects.safe_url import SafeUrl


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


def _attrs(title: str = "TC Title") -> StagedArticleAttributes:
    return StagedArticleAttributes(
        title=title,
        published_at_hint=PublishedAt(datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
        prefer_html_title=False,
    )


async def _make_pending(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
    *,
    attrs: StagedArticleAttributes | None = None,
) -> tuple[SafeUrl, int]:
    """``pending_html_articles`` 行を 1 件作って claim 状態にする。

    Returns:
        (canonical_url, pending_id) — pending は claim 済 (status='running',
        attempt_count=1)。
    """
    safe_url = SafeUrl(url)
    pending_repo = PendingHtmlArticleRepository(db_session)
    pending_id = await pending_repo.create(
        url=safe_url,
        source_id=source.id,
        staged_attributes=attrs or _attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert pending_id is not None
    await db_session.commit()
    # claim して running 状態に遷移 (cron poller の代わり)
    ids = await pending_repo.claim_batch(limit=10, lease_minutes=5)
    await db_session.commit()
    assert pending_id in ids
    return safe_url, pending_id


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    """``ArticleHtmlExtractor.fetch`` を Service の import path 経由で差し替える。"""
    monkeypatch.setattr(
        "app.collection.extraction.content_fetch_service.ArticleHtmlExtractor.fetch",
        mock,
    )


# ---------------------------------------------------------------------------
# 入口ガード
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_for_missing_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """重複配送 (DELETE 済 / 不在 ID) は ``None`` で静かに exit。"""
    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(999_999)
    assert outcome is None


@pytest.mark.asyncio
async def test_returns_none_for_open_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """``status='open'`` (claim されていない) は ``None`` で静かに exit。"""
    safe_url = SafeUrl("https://techcrunch.com/open")
    pending_repo = PendingHtmlArticleRepository(db_session)
    pending_id = await pending_repo.create(
        url=safe_url,
        source_id=tc_source.id,
        staged_attributes=_attrs(),
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    assert pending_id is not None  # status='open' (claim されていない)

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)
    assert outcome is None


# ---------------------------------------------------------------------------
# 成功 path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_returns_article_id_and_persists_article(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExtractedContent + 永続化成功 → ``int`` (article_id) 返却 + Article 1 件作成。"""
    canonical_url, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/article-1"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    article_id = await svc.execute(pending_id)

    assert isinstance(article_id, int)
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1
    assert articles[0].id == article_id
    assert str(articles[0].source_url) == str(canonical_url)


@pytest.mark.asyncio
async def test_success_deletes_pending_in_same_tx(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功時に ``pending_html_articles`` 行は DELETE (articles INSERT と同 tx)。"""
    _, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/article-2"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body="x" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    await svc.execute(pending_id)

    remaining = (
        await db_session.execute(
            select(PendingHtmlArticle).where(PendingHtmlArticle.id == pending_id)
        )
    ).scalar_one_or_none()
    assert remaining is None


@pytest.mark.asyncio
async def test_success_writes_audit_with_body_length_and_canonical_url(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功時 ``pipeline_events`` に SUCCEEDED + body_length + canonical_url が焼かれる."""  # noqa: E501
    body = "x" * 250
    canonical_url, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/article-3"
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(
                title="HTML Title",
                body=body,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    await svc.execute(pending_id)

    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.event_type == "succeeded"
    assert event.outcome_code == "fetched"
    assert event.attempt == 1  # claim 後の attempt_count
    assert event.payload["body_length"] == len(body)
    assert event.payload["canonical_url"] == str(canonical_url)
    assert event.payload["extractor_class"] == "ArticleHtmlExtractor"


# ---------------------------------------------------------------------------
# Permanent / ExtractionEmpty / promotion failure (dropped_terminal 系)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_fetch_error_returns_none_and_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PermanentFetchError → ``None`` + pending status='closed' + audit reason_code."""
    _, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/dead"
    )
    _patch_fetch(monkeypatch, AsyncMock(side_effect=PermanentFetchError("HTTP 404")))

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)

    assert outcome is None
    # Article は作られない
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert articles == []
    # pending は closed
    pending = (
        await db_session.execute(
            select(PendingHtmlArticle).where(PendingHtmlArticle.id == pending_id)
        )
    ).scalar_one()
    assert pending.status == "closed"
    assert pending.leased_until is None
    # audit 記録あり
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.event_type == "skipped"
    assert event.outcome_code == "dropped_terminal"
    assert event.payload["reason_code"] == "permanent_fetch_error"


@pytest.mark.asyncio
async def test_extraction_empty_writes_reason_in_code(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ExtractionEmpty(reason) → ``reason_code='extraction_empty_<reason>'``。"""
    _, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/empty"
    )
    _patch_fetch(
        monkeypatch, AsyncMock(return_value=ExtractionEmpty(reason="not_html"))
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)

    assert outcome is None
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.payload["reason_code"] == "extraction_empty_not_html"


@pytest.mark.asyncio
async def test_promotion_failure_records_quality_gate_metric(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """body はあるが published_at が両方 None → promotion ``ArticleCompletionFailed`` を quality_gate_metric に焼く."""  # noqa: E501
    # published_at_hint=None で staged_attributes を作る
    attrs = StagedArticleAttributes(
        title="Short Title",
        published_at_hint=None,
        prefer_html_title=False,
    )
    _, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/short", attrs=attrs
    )
    _patch_fetch(
        monkeypatch,
        AsyncMock(
            return_value=ExtractedContent(title="OK", body="x" * 200, published_at=None)
        ),
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)

    assert outcome is None
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.payload["reason_code"].startswith("promotion_")
    assert event.payload["quality_gate_metric"]["body_length"] == 200


# ---------------------------------------------------------------------------
# TemporaryFetchError → will_retry / dropped_transient (exhausted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temporary_blip_first_attempt_writes_will_retry(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLIP 1 回目失敗 → ``will_retry`` audit + pending re-open + 未来 ready_at。"""
    _, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/blip"
    )
    _patch_fetch(monkeypatch, AsyncMock(side_effect=ServerErrorBlip("HTTP 502")))

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)

    assert outcome is None

    pending = (
        await db_session.execute(
            select(PendingHtmlArticle).where(PendingHtmlArticle.id == pending_id)
        )
    ).scalar_one()
    assert pending.status == "open"
    assert pending.leased_until is None
    # BLIP 1 回目: 0.5 分後 (= 30 秒後)
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=20) < delta < timedelta(seconds=40)

    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.event_type == "failed"
    assert event.outcome_code == "will_retry"
    assert event.payload["reason_code"] == "temporary_will_retry_blip"
    assert event.error_class is not None


@pytest.mark.asyncio
async def test_temporary_outage_exhausted_writes_dropped_transient(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attempt_count == max_attempts → ``mark_exhausted`` + dropped_transient audit."""
    _, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/outage"
    )
    # OUTAGE_POLICY.max_attempts = 12 を超過させる: attempt_count を 12 に強制セット
    await db_session.execute(
        text("UPDATE pending_html_articles SET attempt_count = 12 WHERE id = :id"),
        {"id": pending_id},
    )
    await db_session.commit()
    _patch_fetch(monkeypatch, AsyncMock(side_effect=ServerErrorOutage("HTTP 503")))

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)

    assert outcome is None

    pending = (
        await db_session.execute(
            select(PendingHtmlArticle).where(PendingHtmlArticle.id == pending_id)
        )
    ).scalar_one()
    assert pending.status == "closed"
    assert pending.leased_until is None

    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.event_type == "failed"
    assert event.outcome_code == "dropped_transient"
    assert event.payload["reason_code"] == "temporary_exhausted_outage"


# ---------------------------------------------------------------------------
# race-loss (conflict_lost)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_lost_returns_none_and_deletes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 worker が article を先に作った → ``None`` + pending DELETE + conflict_lost audit。

    pre-condition: 同 ``source_url`` の Article を直接 INSERT (race の "勝者")。
    """  # noqa: E501
    canonical_url, pending_id = await _make_pending(
        db_session, tc_source, "https://techcrunch.com/race"
    )
    # winner 役の Article を先に INSERT (同一 canonical source_url)
    existing = ArticleORM(
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
            return_value=ExtractedContent(
                title="HTML Title",
                body="z" * 200,
                published_at=PublishedAt(value=datetime(2026, 5, 1, tzinfo=UTC)),
            )
        ),
    )

    svc = ContentFetchService(session_factory)
    outcome = await svc.execute(pending_id)

    assert outcome is None
    # articles は 1 件のまま (敗者は INSERT しない)
    articles = (await db_session.execute(select(ArticleORM))).scalars().all()
    assert len(articles) == 1
    # pending は DELETE
    remaining = (
        await db_session.execute(
            select(PendingHtmlArticle).where(PendingHtmlArticle.id == pending_id)
        )
    ).scalar_one_or_none()
    assert remaining is None
    # audit に conflict_lost (canonical_url 集計 key で関連付く)
    event = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.stage == "content_fetch")
        )
    ).scalar_one()
    assert event.event_type == "skipped"
    assert event.outcome_code == "conflict_lost"
    assert event.payload["canonical_url"] == str(canonical_url)
