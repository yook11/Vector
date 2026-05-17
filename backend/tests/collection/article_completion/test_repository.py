"""``ArticleCompletionRepository`` の統合テスト (実 Postgres)。

Stage 2 completion の永続化境界を検証する。Repository は queue 抽象ではなく、
``pending_html_articles`` に対する処理資格ロード / claim / sweep / 状態遷移を担う。
service には ``status`` / ``ready_at`` / ``leased_until`` を漏らさない。

profile / legacy identity 解決は ``CompletionProfileResolver`` seam に閉じる。
本テストは production 45-registry と非結合にするため stub resolver を注入する。
legacy 行 (旧形 JSONB = ``schemaVersion`` / ``sourceName`` / ``sourceUrl``
不在) でも ``url`` 列 + resolver から identity を注入して完走できることを
固定する (spec §5/§7 後方互換)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.pending_enqueue import PendingHtmlEnqueue
from app.models.news_source import NewsSource
from app.models.pending_html_article import PendingHtmlArticle as PendingHtmlArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.safe_url import SafeUrl
from app.shared.value_objects.source_name import SourceName

_RESOLVED_NAME = SourceName("Resolved Source")


class _StubResolver:
    """``CompletionProfileResolver`` の stub (production 45-registry と非結合)。

    legacy 行の ``resolve_name`` 解決と profile 解決を固定値で返し、本テストを
    DB の ``news_sources`` 引き / ``SOURCES`` 構成から独立させる。
    """

    def __init__(self, profile: SourceCompletionProfile = DEFAULT_PROFILE) -> None:
        self._profile = profile

    async def resolve(
        self, *, source_id: int, source_name: SourceName | None
    ) -> SourceCompletionProfile:
        return self._profile

    async def resolve_name(self, *, source_id: int) -> SourceName:
        return _RESOLVED_NAME


def _repo(db_session: AsyncSession) -> ArticleCompletionRepository:
    return ArticleCompletionRepository(db_session, _StubResolver())


def _observed(*, url: str, title: str = "Sample") -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName("Sample Source"),
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value=title, origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )


def _attrs() -> dict:
    """新形 (``ObservedArticle``) の JSONB 表現 (直接 INSERT 用)。"""
    return _observed(
        url="https://example.com/p/staged", title="Pending Title"
    ).model_dump(mode="json", by_alias=True)


def _legacy_attrs() -> dict:
    """旧形 (legacy) JSONB の wire 表現 (schemaVersion / identity 不在)。

    旧 ``StagedArticleAttributes.model_dump(mode="json")`` が出力していた wire
    形を literal で固定する。型が消えても **後方互換契約 (in-flight 旧行が
    Stage 2 を完走できる)** を回帰検出するため、コードでなく wire 契約を
    pin する (memory ``feedback_test_invariants_over_change_tracking``)。
    """
    return {
        "title": "Legacy Title",
        "published_at_hint": {"value": "2026-05-01T00:00:00Z"},
        "prefer_html_title": False,
    }


async def _enqueue(
    db_session: AsyncSession,
    *,
    source_id: int,
    url: str,
    title: str = "Sample",
    ready_at: datetime,
) -> int:
    """Stage 1 投入で ``status='open'`` の pending を 1 件作る。"""
    pending_id = await PendingHtmlEnqueue(db_session).enqueue(
        _observed(url=url, title=title),
        source_id=source_id,
        ready_at=ready_at,
    )
    assert pending_id is not None
    return pending_id


async def _make_running(
    db_session: AsyncSession,
    *,
    source_id: int,
    url: str,
    ready_at: datetime,
    leased_until: datetime,
    attempt_count: int = 1,
    staged: dict | None = None,
) -> int:
    pending = PendingHtmlArticleORM(
        url=SafeUrl(url),
        source_id=source_id,
        status="running",
        staged_attributes=staged if staged is not None else _attrs(),
        ready_at=ready_at,
        leased_until=leased_until,
        attempt_count=attempt_count,
    )
    db_session.add(pending)
    await db_session.commit()
    await db_session.refresh(pending)
    return pending.id


async def _select_pending(
    db_session: AsyncSession, pending_id: int
) -> PendingHtmlArticleORM:
    row = (
        await db_session.execute(
            select(PendingHtmlArticleORM).where(PendingHtmlArticleORM.id == pending_id)
        )
    ).scalar_one()
    await db_session.refresh(row)
    return row


async def _claim_one(
    db_session: AsyncSession,
    pending_id: int,
    *,
    now: datetime | None = None,
) -> ReadyForArticleCompletion:
    claim_now = now or datetime.now(UTC)
    repository = _repo(db_session)
    ids = await repository.claim_ready_batch(
        limit=10,
        now=claim_now,
        leased_until=claim_now + timedelta(minutes=5),
    )
    await db_session.commit()
    assert pending_id in ids
    ready = await repository.try_load_for_completion(pending_id)
    assert ready is not None
    return ready


# ---------------------------------------------------------------------------
# try_load_for_completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_load_for_completion_returns_claimed_target(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``status='running'`` の行だけが completion target として物体化される。"""
    url = CanonicalArticleUrl("https://example.com/p/find")
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url=str(url),
        title="Find Me",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    target = await _claim_one(db_session, pending_id)

    assert target.pending_id == pending_id
    assert target.source_id == sample_source.id
    assert target.attempt_count == 1
    assert target.observed.title is not None
    assert target.observed.title.value == "Find Me"
    assert target.source_url == url
    assert target.profile is DEFAULT_PROFILE


@pytest.mark.asyncio
async def test_try_load_injects_identity_for_legacy_jsonb_row(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """旧形 JSONB (schemaVersion / sourceName / sourceUrl 不在) でも、
    ``url`` 列 + resolver から identity を注入して Ready を完走できる。"""
    url = "https://example.com/p/legacy"
    pending_id = await _make_running(
        db_session,
        source_id=sample_source.id,
        url=url,
        ready_at=datetime.now(UTC) - timedelta(minutes=1),
        leased_until=datetime.now(UTC) + timedelta(minutes=5),
        staged=_legacy_attrs(),
    )

    ready = await _repo(db_session).try_load_for_completion(pending_id)

    assert ready is not None
    # source_url は url 列 (authoritative) から、source_name は resolver から注入
    assert ready.source_url == CanonicalArticleUrl(url)
    assert ready.observed.source_name == _RESOLVED_NAME
    assert ready.observed.title is not None
    assert ready.observed.title.value == "Legacy Title"
    assert ready.observed.body is None  # 旧形は body を持たない
    assert ready.observed.published_at is not None


@pytest.mark.asyncio
async def test_try_load_for_completion_returns_none_for_missing_or_open(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repository = _repo(db_session)
    assert await repository.try_load_for_completion(999999) is None

    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/open",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    assert await repository.try_load_for_completion(pending_id) is None


# ---------------------------------------------------------------------------
# claim_ready_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_ready_batch_picks_only_open_ready(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``status='open' AND ready_at <= now`` のみ claim 対象。"""
    now = datetime.now(UTC)
    ready_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/ready",
        ready_at=now - timedelta(minutes=1),
    )
    await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/future",
        ready_at=now + timedelta(minutes=10),
    )
    await db_session.commit()

    ids = await _repo(db_session).claim_ready_batch(
        limit=10,
        now=now,
        leased_until=now + timedelta(minutes=5),
    )
    await db_session.commit()

    assert ids == [ready_id]


@pytest.mark.asyncio
async def test_claim_ready_batch_advances_state_atomically(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """claim で running 化 + leased_until 設定 + attempt_count++ が一括適用される."""
    now = datetime.now(UTC)
    leased_until = now + timedelta(minutes=5)
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/claim-state",
        ready_at=now - timedelta(seconds=1),
    )
    await db_session.commit()

    ids = await _repo(db_session).claim_ready_batch(
        limit=10,
        now=now,
        leased_until=leased_until,
    )
    await db_session.commit()

    assert ids == [pending_id]
    row = await _select_pending(db_session, pending_id)
    assert row.status == "running"
    assert row.leased_until == leased_until
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_claim_ready_batch_respects_limit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    now = datetime.now(UTC)
    for i in range(5):
        await _enqueue(
            db_session,
            source_id=sample_source.id,
            url=f"https://example.com/p/limit-{i}",
            ready_at=now - timedelta(seconds=1),
        )
    await db_session.commit()

    ids = await _repo(db_session).claim_ready_batch(
        limit=2,
        now=now,
        leased_until=now + timedelta(minutes=5),
    )
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_concurrent_claim_ready_batch_skips_locked(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """並行 claim は FOR UPDATE SKIP LOCKED で同じ行を二重 claim しない."""
    now = datetime.now(UTC)
    created_ids: list[int] = []
    for i in range(4):
        pid = await _enqueue(
            db_session,
            source_id=sample_source.id,
            url=f"https://example.com/p/race-{i}",
            ready_at=now - timedelta(seconds=1),
        )
        created_ids.append(pid)
    await db_session.commit()

    async def _claim_in_new_session() -> list[int]:
        async with session_factory() as session:
            ids = await ArticleCompletionRepository(
                session, _StubResolver()
            ).claim_ready_batch(
                limit=10,
                now=now,
                leased_until=now + timedelta(minutes=5),
            )
            await session.commit()
            return ids

    results = await asyncio.gather(
        _claim_in_new_session(),
        _claim_in_new_session(),
    )
    flat = [pid for chunk in results for pid in chunk]
    assert sorted(flat) == sorted(created_ids)
    assert len(flat) == len(set(flat))


# ---------------------------------------------------------------------------
# sweep_expired_leases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_expired_leases_reopens_dead_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """死んだ lease (running + leased_until <= now) は ``open`` に戻される。"""
    now = datetime.now(UTC)
    pending_id = await _make_running(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/sweep",
        ready_at=now - timedelta(minutes=5),
        leased_until=now - timedelta(seconds=1),
    )

    swept = await _repo(db_session).sweep_expired_leases(now=now)
    await db_session.commit()

    assert swept == 1
    row = await _select_pending(db_session, pending_id)
    assert row.status == "open"
    assert row.ready_at == now
    assert row.leased_until is None


@pytest.mark.asyncio
async def test_sweep_expired_leases_leaves_live_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """生きている lease (leased_until > now) は触らない。"""
    now = datetime.now(UTC)
    pending_id = await _make_running(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/sweep-live",
        ready_at=now - timedelta(minutes=1),
        leased_until=now + timedelta(minutes=5),
    )

    swept = await _repo(db_session).sweep_expired_leases(now=now)

    assert swept == 0
    assert (await _select_pending(db_session, pending_id)).status == "running"


# ---------------------------------------------------------------------------
# close_claimed / schedule_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_claimed_closes_pending(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/terminal",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, pending_id)
    now = datetime.now(UTC)

    updated = await _repo(db_session).close_claimed(target, now=now)
    await db_session.commit()

    assert updated is True
    row = await _select_pending(db_session, pending_id)
    assert row.status == "closed"
    assert row.leased_until is None


@pytest.mark.asyncio
async def test_schedule_retry_reopens_with_future_ready_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """一時失敗で ``open`` + 未来 ``ready_at`` + ``leased_until=NULL`` に戻る。"""
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/retry",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, pending_id)
    now = datetime.now(UTC)
    next_at = now + timedelta(minutes=15)

    updated = await _repo(db_session).schedule_retry(target, ready_at=next_at, now=now)
    await db_session.commit()

    assert updated is True
    row = await _select_pending(db_session, pending_id)
    assert row.status == "open"
    assert row.leased_until is None
    assert row.ready_at == next_at


@pytest.mark.asyncio
async def test_state_transitions_ignore_stale_attempt(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """attempt_count が変わった古い worker は現在の claim を閉じられない。"""
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        url="https://example.com/p/stale",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, pending_id)
    await db_session.execute(
        update(PendingHtmlArticleORM)
        .where(PendingHtmlArticleORM.id == pending_id)
        .values(attempt_count=target.attempt_count + 1)
    )
    await db_session.commit()

    updated = await _repo(db_session).close_claimed(target, now=datetime.now(UTC))
    await db_session.commit()

    assert updated is False
    row = await _select_pending(db_session, pending_id)
    assert row.status == "running"
    assert row.attempt_count == target.attempt_count + 1
