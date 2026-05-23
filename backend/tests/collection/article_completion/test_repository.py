"""``ArticleCompletionRepository`` の統合テスト (実 Postgres)。

Stage 2 completion の永続化境界を検証する。Repository は queue 抽象ではなく、
``incomplete_articles`` に対する処理資格ロード / claim / sweep / 状態遷移を担う。
service には ``status`` / ``ready_at`` / ``leased_until`` を漏らさない。

identity 解決は表層列 (``url`` / ``source_name``) から直接 hydrate する
(spec ``Pending source identity refactor.md`` Chunk 4)。profile は
``SOURCES[observed.source_name].completion_policy`` 直叩きで、registry 未登録
source は ``KeyError`` 伝播 (``[[feedback_failure_visibility]]``)。
production 45-registry と非結合にするため、profile を上書きする test では
``monkeypatch.setitem(SOURCES, ...)`` で運搬体を差し替える。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.repository import IncompleteArticleRepository
from app.collection.source_fetch.strategy import SOURCES
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    HTML_TITLE_POLICY,
    ArticleCompletionPolicy,
)
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl
from app.shared.value_objects.source_name import SourceName


@dataclass(frozen=True)
class _StubArticleSource:
    """``ArticleSource`` Protocol の test 用最小実装。

    repository は ``completion_policy`` のみ参照する。``collect`` は本テストで
    呼ばれないが Protocol shape を満たすため no-op generator を残す。
    ``monkeypatch.setitem(SOURCES, name, _StubArticleSource(...))`` で
    profile を test 単位に差し替えるための運搬体 (production registry には
    登録しない)。
    """

    name: SourceName
    completion_policy: ArticleCompletionPolicy
    endpoint_url: str = "https://example.com/feed"
    observed_origin: ObservedOrigin = ObservedOrigin.feed

    async def collect(self, tools: FetchTools) -> AsyncIterator[FetchedArticle]:
        # 本テストでは呼ばれない (Protocol shape のため空 generator)。
        if False:
            yield  # pragma: no cover


def _repo(db_session: AsyncSession) -> ArticleCompletionRepository:
    return ArticleCompletionRepository(db_session)


@pytest.fixture(autouse=True)
def _register_sample_source_in_registry(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sample_source`` を使う test で SOURCES に ``DEFAULT_POLICY`` を登録する。

    repository が ``SOURCES[observed.source_name]`` 直叩きで profile を引く
    ため、production registry に未登録の test fixture (``sample_source.name =
    'Test Tech Source'``) では KeyError が起きる。本 fixture は production
    と整合する形 (DEFAULT_POLICY) を default として SOURCES に挿入し、test が
    profile を上書きしたい場合は test 内で ``monkeypatch.setitem`` を再度
    呼べば override できる。``sample_source`` を要求しない test (例:
    missing_or_open 999999 path) には何もしない。
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
        url="https://example.com/p/staged",
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
    pending_id = await IncompleteArticleRepository(db_session).save(
        _observed(url=url, source_name=source_name, title=title),
        source_id=source_id,
        ready_at=ready_at,
    )
    if pending_id is None:
        # ``UNIQUE(url)`` 違反 = test の setup precondition 違反 (fixture が
        # 壊れている / 同一 URL の重複)。test assertion と弁別するため raise。
        msg = f"setup precondition violated: enqueue returned None for url={url}"
        raise RuntimeError(msg)
    return pending_id


async def _make_running(
    db_session: AsyncSession,
    *,
    source_id: int,
    source_name: SourceName,
    url: str,
    ready_at: datetime,
    leased_until: datetime,
    attempt_count: int = 1,
    staged: dict | None = None,
) -> int:
    pending = IncompleteArticleORM(
        url=SafeUrl(url),
        source_id=source_id,
        source_name=source_name,
        status="running",
        staged_attributes=staged if staged is not None else _attrs(source_name),
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
) -> IncompleteArticleORM:
    row = (
        await db_session.execute(
            select(IncompleteArticleORM).where(IncompleteArticleORM.id == pending_id)
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
    if pending_id not in ids:
        msg = (
            f"setup precondition violated: claim_ready_batch did not pick "
            f"pending_id={pending_id} (picked={ids})"
        )
        raise RuntimeError(msg)
    ready = await repository.try_load_for_completion(pending_id)
    if ready is None:
        msg = (
            "setup precondition violated: try_load returned None after claim "
            f"(id={pending_id})"
        )
        raise RuntimeError(msg)
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
        source_name=sample_source.name,
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


@pytest.mark.asyncio
async def test_try_load_for_completion_returns_none_for_missing_or_open(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    repository = _repo(db_session)
    assert await repository.try_load_for_completion(999999) is None

    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/open",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()

    assert await repository.try_load_for_completion(pending_id) is None


@pytest.mark.asyncio
async def test_try_load_resolves_profile_from_source_name_registry(
    db_session: AsyncSession,
    sample_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec #8: profile は ``SOURCES[observed.source_name]`` から直接引かれる。

    repository の旧 DI seam (resolver adapter) が消えた後、表層列
    ``source_name`` 経由で SOURCES を引いた値がそのまま ``ready.profile`` に
    流れることを動作で pin する (registry を test 単位で差し替えて、
    ``HTML_TITLE_POLICY`` が ``DEFAULT_POLICY`` と区別される寄り辺を作る)。
    """
    monkeypatch.setitem(
        SOURCES,
        sample_source.name,
        _StubArticleSource(
            name=sample_source.name, completion_policy=HTML_TITLE_POLICY
        ),
    )
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/profile-registry",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    ready = await _claim_one(db_session, pending_id)

    assert ready.profile is HTML_TITLE_POLICY


@pytest.mark.asyncio
async def test_try_load_raises_keyerror_for_unregistered_source(
    db_session: AsyncSession,
    sample_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec #9b: registry 未登録 source は ``KeyError`` で上位に伝播する。

    ``DEFAULT_POLICY`` fallback (旧 ``SOURCES.get(name) or DEFAULT_POLICY``)
    は drift 隠蔽になるため明示的に廃止 (``[[feedback_failure_visibility]]``)。
    """
    # まず Stage 1 投入 (sample_source は SOURCES 未登録だが、enqueue は
    # composite FK のみで成立する)。
    pending_id = await _enqueue(
        db_session,
        source_id=sample_source.id,
        source_name=sample_source.name,
        url="https://example.com/p/drift",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    # SOURCES に万一登録があっても fail-fast を強制するため、明示的に消す。
    monkeypatch.delitem(SOURCES, sample_source.name, raising=False)
    # claim までは通る (DB 更新のみ)。try_load で profile lookup が走る。
    repository = _repo(db_session)
    claim_now = datetime.now(UTC)
    ids = await repository.claim_ready_batch(
        limit=10,
        now=claim_now,
        leased_until=claim_now + timedelta(minutes=5),
    )
    await db_session.commit()
    assert pending_id in ids

    with pytest.raises(KeyError):
        await repository.try_load_for_completion(pending_id)


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
    pending_id = await _enqueue(
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
        source_name=sample_source.name,
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
        source_name=sample_source.name,
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
        source_name=sample_source.name,
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
        source_name=sample_source.name,
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
        source_name=sample_source.name,
        url="https://example.com/p/stale",
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await db_session.commit()
    target = await _claim_one(db_session, pending_id)
    await db_session.execute(
        update(IncompleteArticleORM)
        .where(IncompleteArticleORM.id == pending_id)
        .values(attempt_count=target.attempt_count + 1)
    )
    await db_session.commit()

    updated = await _repo(db_session).close_claimed(target, now=datetime.now(UTC))
    await db_session.commit()

    assert updated is False
    row = await _select_pending(db_session, pending_id)
    assert row.status == "running"
    assert row.attempt_count == target.attempt_count + 1
