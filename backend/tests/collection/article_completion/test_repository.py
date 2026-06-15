"""``ArticleCompletionRepository`` の統合テスト (実 Postgres)。

Stage 2 completion の永続化境界を検証する。Repository は queue 抽象ではなく、
``incomplete_articles`` に対する Ready 構築 facts / claim / sweep / 状態遷移を担う。
service には ``status`` / ``ready_at`` / ``leased_until`` を漏らさない。

Ready 構築可否や profile 解決は domain / source registry helper の責務。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_acquisition.strategy import SOURCES
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.source_name import SourceName
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM
from app.models.news_source import NewsSource
from app.shared.security.safe_url import SafeUrl


@dataclass(frozen=True)
class _StubArticleSource(BaseArticleSource):
    """``ArticleSource`` Protocol の test 用最小実装。

    Ready 構築 helper は ``completion_policy`` のみ参照する。``read`` / ``map_entry``
    は本テストで呼ばれないが Protocol shape を満たすため no-op を残す
    (in_scope/select は ``BaseArticleSource``)。
    ``monkeypatch.setitem(SOURCES, name, _StubArticleSource(...))`` で
    policy を test 単位に差し替えるための運搬体 (production registry には
    登録しない)。
    """

    name: SourceName
    completion_policy: ArticleCompletionPolicy
    endpoint_url: str = "https://example.com/feed"
    observed_origin: ObservedOrigin = ObservedOrigin.feed

    async def read(self, tools: ReaderTools) -> list[FetchedArticle]:  # noqa: ARG002
        return []

    def map_entry(self, entry: FetchedArticle) -> FetchedArticle:
        return entry


def _repo(db_session: AsyncSession) -> ArticleCompletionRepository:
    return ArticleCompletionRepository(db_session)


@pytest.fixture(autouse=True)
def _register_sample_source_in_registry(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sample_source`` を使う Ready 構築 helper 用に policy を登録する。

    repository 自体は ``SOURCES`` を参照しない。state transition test の setup で
    domain Ready を作るため、test fixture の source 名に default policy を挿入する。
    """
    if "sample_source" not in request.fixturenames:
        return
    sample_source = request.getfixturevalue("sample_source")
    monkeypatch.setitem(
        SOURCES,
        sample_source.name,
        _StubArticleSource(
            name=sample_source.name,
            completion_policy=DEFAULT_POLICY,
        ),
    )


def _observed(
    *, url: str, source_name: SourceName, title: str = "Sample"
) -> ObservedArticle:
    return ObservedArticle(
        source_name=source_name,
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value=title, origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )


def _attrs(source_name: SourceName) -> dict:
    """新形 (``ObservedArticle``) の JSONB 表現 (直接 INSERT 用)。

    identity (``sourceName`` / ``source_url``) は ``Field(exclude=True)`` で
    JSONB から除外されるため、表層列 (``source_name`` / ``url``) と同値の
    ``source_name`` を渡しても JSONB には焼かれない。
    """
    return _observed(
        url="https://example.com/p/observed_payload",
        source_name=source_name,
        title="Pending Title",
    ).model_dump(mode="json", by_alias=True)


async def _enqueue(
    db_session: AsyncSession,
    *,
    source_id: int,
    source_name: SourceName,
    url: str,
    title: str = "Sample",
    ready_at: datetime,
) -> int:
    """Stage 1 投入で ``status='open'`` の pending を 1 件作る。"""
    incomplete_article_id = await IncompleteArticleRepository(db_session).save(
        _observed(url=url, source_name=source_name, title=title),
        source_id=source_id,
        ready_at=ready_at,
    )
    if incomplete_article_id is None:
        # ``UNIQUE(url)`` 違反 = test の setup precondition 違反 (fixture が
        # 壊れている / 同一 URL の重複)。test assertion と弁別するため raise。
        msg = f"setup precondition violated: enqueue returned None for url={url}"
        raise RuntimeError(msg)
    return incomplete_article_id


async def _make_running(
    db_session: AsyncSession,
    *,
    source_id: int,
    source_name: SourceName,
    url: str,
    ready_at: datetime,
    leased_until: datetime,
    attempt_count: int = 1,
    observed_payload: dict | None = None,
) -> int:
    pending = IncompleteArticleORM(
        url=SafeUrl(url),
        source_id=source_id,
        source_name=source_name,
        status="running",
        observed_article=(
            observed_payload if observed_payload is not None else _attrs(source_name)
        ),
        ready_at=ready_at,
        leased_until=leased_until,
        attempt_count=attempt_count,
    )
    db_session.add(pending)
    await db_session.commit()
    await db_session.refresh(pending)
    return pending.id


async def _select_pending(
    db_session: AsyncSession, incomplete_article_id: int
) -> IncompleteArticleORM:
    row = (
        await db_session.execute(
            select(IncompleteArticleORM).where(
                IncompleteArticleORM.id == incomplete_article_id
            )
        )
    ).scalar_one()
    await db_session.refresh(row)
    return row


async def _claim_one(
    db_session: AsyncSession,
    incomplete_article_id: int,
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
    if incomplete_article_id not in ids:
        msg = (
            f"setup precondition violated: claim_ready_batch did not pick "
            f"incomplete_article_id={incomplete_article_id} (picked={ids})"
        )
        raise RuntimeError(msg)
    return await ReadyForArticleCompletion.try_advance_from(
        incomplete_article_id=incomplete_article_id,
        repo=repository,
    )


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_claimed_target(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """pending 行の Ready 構築 facts を status ごと返す。"""
    url = CanonicalArticleUrl("https://example.com/p/find")
    incomplete_article_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url=str(url),
        title="Find Me",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    await _claim_one(db_session, incomplete_article_id)
    target = await _repo(db_session).load_ready_build_facts(incomplete_article_id)

    assert target is not None
    assert target.incomplete_article_id == incomplete_article_id
    assert target.source_id == sample_source.id
    assert target.status == "running"
    assert target.attempt_count == 1
    assert target.observed_article["title"]["value"] == "Find Me"
    assert target.source_url == str(url)


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_none_for_missing(
    db_session: AsyncSession,
) -> None:
    repository = _repo(db_session)
    assert await repository.load_ready_build_facts(999999) is None


@pytest.mark.asyncio
async def test_load_ready_build_facts_returns_open_status(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repository = _repo(db_session)

    incomplete_article_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/open",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    target = await repository.load_ready_build_facts(incomplete_article_id)
    assert target is not None
    assert target.status == "open"


@pytest.mark.asyncio
async def test_claim_ready_batch_picks_only_open_ready(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``status='open' AND ready_at <= now`` のみ claim 対象。"""
    now = datetime.now(UTC)
    ready_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/ready",
        ready_at=now - timedelta(minutes=1),
    )
    await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
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
    incomplete_article_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
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

    assert ids == [incomplete_article_id]
    row = await _select_pending(db_session, incomplete_article_id)
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
            source_name=sample_source.name,
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
            source_name=sample_source.name,
            url=f"https://example.com/p/race-{i}",
            ready_at=now - timedelta(seconds=1),
        )
        created_ids.append(pid)
    await db_session.commit()

    async def _claim_in_new_session() -> list[int]:
        async with session_factory() as session:
            ids = await ArticleCompletionRepository(session).claim_ready_batch(
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


@pytest.mark.asyncio
async def test_sweep_expired_leases_reopens_dead_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """死んだ lease (running + leased_until <= now) は ``open`` に戻される。"""
    now = datetime.now(UTC)
    incomplete_article_id = await _make_running(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/sweep",
        ready_at=now - timedelta(minutes=5),
        leased_until=now - timedelta(seconds=1),
    )

    swept = await _repo(db_session).sweep_expired_leases(now=now)
    await db_session.commit()

    assert swept == 1
    row = await _select_pending(db_session, incomplete_article_id)
    assert row.status == "open"
    assert row.ready_at == now
    assert row.leased_until is None


@pytest.mark.asyncio
async def test_sweep_expired_leases_leaves_live_lease(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """生きている lease (leased_until > now) は触らない。"""
    now = datetime.now(UTC)
    incomplete_article_id = await _make_running(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/sweep-live",
        ready_at=now - timedelta(minutes=1),
        leased_until=now + timedelta(minutes=5),
    )

    swept = await _repo(db_session).sweep_expired_leases(now=now)

    assert swept == 0
    assert (
        await _select_pending(db_session, incomplete_article_id)
    ).status == "running"


@pytest.mark.asyncio
async def test_close_claimed_closes_pending(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    incomplete_article_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/terminal",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, incomplete_article_id)
    now = datetime.now(UTC)

    updated = await _repo(db_session).close_claimed(target, now=now)
    await db_session.commit()

    assert updated is True
    row = await _select_pending(db_session, incomplete_article_id)
    assert row.status == "closed"
    assert row.leased_until is None


@pytest.mark.asyncio
async def test_schedule_retry_reopens_with_future_ready_at(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """一時失敗で ``open`` + 未来 ``ready_at`` + ``leased_until=NULL`` に戻る。"""
    incomplete_article_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/retry",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, incomplete_article_id)
    now = datetime.now(UTC)
    next_at = now + timedelta(minutes=15)

    updated = await _repo(db_session).schedule_retry(target, ready_at=next_at, now=now)
    await db_session.commit()

    assert updated is True
    row = await _select_pending(db_session, incomplete_article_id)
    assert row.status == "open"
    assert row.leased_until is None
    assert row.ready_at == next_at


@pytest.mark.asyncio
async def test_state_transitions_ignore_stale_attempt(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """attempt_count が変わった古い worker は現在の claim を閉じられない。"""
    incomplete_article_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/stale",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, incomplete_article_id)
    await db_session.execute(
        update(IncompleteArticleORM)
        .where(IncompleteArticleORM.id == incomplete_article_id)
        .values(attempt_count=target.attempt_count + 1)
    )
    await db_session.commit()

    updated = await _repo(db_session).close_claimed(target, now=datetime.now(UTC))
    await db_session.commit()

    assert updated is False
    row = await _select_pending(db_session, incomplete_article_id)
    assert row.status == "running"
    assert row.attempt_count == target.attempt_count + 1
