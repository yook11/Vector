"""``dispatch.py`` cron task の振る舞い不変条件テスト (PR2.5-B Block 3)。

検証する不変条件 (持続可能テスト方針):

- ``dispatch_html_fetch_jobs``:
  - ``status='open' AND ready_at <= NOW`` の pending のみ claim される
    (時間軸 ``ready_at`` × 状態軸 ``status`` の選別境界)
  - claim された pending は ``status='running'`` + ``leased_until`` が将来 +
    ``attempt_count++`` に遷移する (副作用の不変条件を 1 ケースに圧縮)
  - ``_DISPATCH_BATCH_LIMIT`` を超える件数があっても LIMIT 件のみ dispatch
  - ``scrape_html_body.kiq`` が claim 済 pending_id 列で正確に呼ばれる
  - 候補ゼロでも空 tick (dispatched_count=0) として正常終了

- ``sweep_expired_leases``:
  - ``status='running' AND leased_until <= NOW`` のみ ``open`` に戻る
  - active lease (``leased_until > NOW``) と非 running 状態は不変
  - 二重起動 idempotent (1 度戻したものは 2 度目で 0 件)

テスト方針 (持続可能性):
- pending を直接 INSERT し、Fetcher / NewsSource / ArticleAcquisitionService 経由しない
- ソースが増えても破綻しない (横軸独立)
- ``scrape_html_body.kiq`` は AsyncMock で「呼ばれた回数 + 引数」のみ確認
- 内部実装 (private method / log message / 中間 dict) は assert しない
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.source_name import SourceName
from app.models.incomplete_article import IncompleteArticle as IncompleteArticleORM
from app.models.news_source import NewsSource
from app.queue.tasks import completion as dispatch_module
from app.queue.tasks.completion import (
    dispatch_html_fetch_jobs,
    sweep_expired_leases,
)
from app.shared.security.safe_url import SafeUrl


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """taskiq Context の最小 mock。task は session_factory のみ参照する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


def _observed(
    source_name: SourceName,
    url: str,
    title: str = "Pending Title",
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


def _attrs(
    source_name: SourceName,
    url: str = "https://example.com/disp/staged",
) -> dict:
    return _observed(source_name, url).to_staged_attributes()


async def _make_pending(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    url: str,
    status: str,
    ready_at: datetime | None,
    leased_until: datetime | None = None,
    attempt_count: int = 0,
) -> int:
    """1 件の pending を任意状態で直接 INSERT する。

    CHECK 制約 (status × leased_until / status × ready_at) と整合する組合せのみ
    呼び出し側が指定する。``url`` UNIQUE のため URL は test 内で一意に。
    """
    safe_url = SafeUrl(url)

    # status='open' は enqueue で作る (CHECK 整合・JSONB serialization 込)
    if status == "open":
        enqueue = IncompleteArticleRepository(db_session)
        pending_id = await enqueue.save(
            _observed(source.name, url),
            source_id=source.id,
            ready_at=ready_at or datetime.now(UTC),
        )
        assert pending_id is not None
        if attempt_count != 0:
            await db_session.execute(
                update(IncompleteArticleORM)
                .where(IncompleteArticleORM.id == pending_id)
                .values(attempt_count=attempt_count)
            )
        await db_session.commit()
        return pending_id

    # status='running' / 'closed' は ORM 直接組み立て (CHECK 制約整合)
    pending = IncompleteArticleORM(
        url=safe_url,
        source_id=source.id,
        source_name=source.name,
        status=status,
        staged_attributes=_attrs(source.name, url),
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
    """pending を id で再 SELECT (post-condition 確認用)。"""
    row = (
        await db_session.execute(
            select(IncompleteArticleORM).where(IncompleteArticleORM.id == pending_id)
        )
    ).scalar_one()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_dispatches_only_open_and_ready_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``status='open' AND ready_at <= NOW`` の 2 件のみ claim され、kiq 投入される。

    時間軸 (ready_at 過去/未来) と状態軸 (open/running/closed) を 1 ケースに統合。
    """
    now = datetime.now(UTC)
    past = now - timedelta(seconds=1)
    future = now + timedelta(minutes=10)

    ready_id_a = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/ready-a",
        status="open",
        ready_at=past,
    )
    ready_id_b = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/ready-b",
        status="open",
        ready_at=past,
    )
    not_ready_id = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/not-ready",
        status="open",
        ready_at=future,
    )
    running_id = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/running",
        status="running",
        ready_at=past,
        leased_until=future,
    )
    closed_id = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/closed",
        status="closed",
        ready_at=None,
        leased_until=None,
    )

    kiq_mock = AsyncMock()
    monkeypatch.setattr("app.queue.tasks.completion.scrape_html_body.kiq", kiq_mock)

    result = await dispatch_html_fetch_jobs(ctx=_ctx(session_factory))

    assert result == {"dispatched_count": 2}
    dispatched_ids = {call.args[0] for call in kiq_mock.await_args_list}
    assert dispatched_ids == {ready_id_a, ready_id_b}

    # claim された 2 件は running に遷移、それ以外は不変
    for pid in (ready_id_a, ready_id_b):
        row = await _select_pending(db_session, pid)
        assert row.status == "running"
        assert row.leased_until is not None and row.leased_until > now
    assert (await _select_pending(db_session, not_ready_id)).status == "open"
    assert (await _select_pending(db_session, running_id)).status == "running"
    assert (await _select_pending(db_session, closed_id)).status == "closed"


@pytest.mark.asyncio
async def test_increments_attempt_count_and_sets_lease(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim 後 ``attempt_count++`` + ``leased_until ≈ NOW + lease_minutes`` に遷移。"""
    past = datetime.now(UTC) - timedelta(seconds=1)
    pending_id = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/attempt",
        status="open",
        ready_at=past,
        attempt_count=2,
    )

    monkeypatch.setattr("app.queue.tasks.completion.scrape_html_body.kiq", AsyncMock())

    before = datetime.now(UTC)
    await dispatch_html_fetch_jobs(ctx=_ctx(session_factory))
    after = datetime.now(UTC)

    row = await _select_pending(db_session, pending_id)
    assert row.attempt_count == 3
    assert row.leased_until is not None
    expected_min = (
        before
        + timedelta(minutes=dispatch_module._LEASE_MINUTES)
        - timedelta(seconds=2)
    )
    expected_max = (
        after + timedelta(minutes=dispatch_module._LEASE_MINUTES) + timedelta(seconds=2)
    )
    assert expected_min <= row.leased_until <= expected_max


@pytest.mark.asyncio
async def test_respects_dispatch_batch_limit(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready 件数が ``_DISPATCH_BATCH_LIMIT`` を超えても LIMIT 件のみ dispatch。"""
    monkeypatch.setattr(dispatch_module, "_DISPATCH_BATCH_LIMIT", 2)
    past = datetime.now(UTC) - timedelta(seconds=1)
    ids = [
        await _make_pending(
            db_session,
            sample_source,
            url=f"https://example.com/disp/limit-{i}",
            status="open",
            ready_at=past,
        )
        for i in range(3)
    ]

    kiq_mock = AsyncMock()
    monkeypatch.setattr("app.queue.tasks.completion.scrape_html_body.kiq", kiq_mock)

    result = await dispatch_html_fetch_jobs(ctx=_ctx(session_factory))

    assert result == {"dispatched_count": 2}
    assert kiq_mock.await_count == 2
    statuses = [(await _select_pending(db_session, pid)).status for pid in ids]
    assert sorted(statuses) == ["open", "running", "running"]


@pytest.mark.asyncio
async def test_returns_zero_when_no_ready_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready 候補ゼロの空 tick で kiq を呼ばず ``dispatched_count=0`` を返す。"""
    future = datetime.now(UTC) + timedelta(minutes=10)
    await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/zero-future",
        status="open",
        ready_at=future,
    )
    await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/disp/zero-closed",
        status="closed",
        ready_at=None,
    )

    kiq_mock = AsyncMock()
    monkeypatch.setattr("app.queue.tasks.completion.scrape_html_body.kiq", kiq_mock)

    result = await dispatch_html_fetch_jobs(ctx=_ctx(session_factory))

    assert result == {"dispatched_count": 0}
    kiq_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweeps_running_with_expired_lease(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """``running`` + ``leased_until <= NOW`` の行が ``open`` に戻り、件数が返る。"""
    now = datetime.now(UTC)
    past_lease = now - timedelta(seconds=1)
    pid_a = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/sweep/a",
        status="running",
        ready_at=now - timedelta(minutes=5),
        leased_until=past_lease,
    )
    pid_b = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/sweep/b",
        status="running",
        ready_at=now - timedelta(minutes=5),
        leased_until=past_lease,
    )

    result = await sweep_expired_leases(ctx=_ctx(session_factory))

    assert result == {"swept_count": 2}
    for pid in (pid_a, pid_b):
        row = await _select_pending(db_session, pid)
        assert row.status == "open"
        assert row.leased_until is None
        assert row.ready_at is not None and row.ready_at >= now - timedelta(seconds=2)


@pytest.mark.asyncio
async def test_skips_active_lease_and_non_running_status(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """active lease + open + closed は touch されず、戻り値も 0。"""
    now = datetime.now(UTC)
    future_lease = now + timedelta(minutes=10)
    active_running = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/sweep/active",
        status="running",
        ready_at=now - timedelta(minutes=1),
        leased_until=future_lease,
    )
    open_id = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/sweep/open",
        status="open",
        ready_at=now,
    )
    closed_id = await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/sweep/closed",
        status="closed",
        ready_at=None,
    )

    result = await sweep_expired_leases(ctx=_ctx(session_factory))

    assert result == {"swept_count": 0}
    assert (await _select_pending(db_session, active_running)).status == "running"
    assert (await _select_pending(db_session, open_id)).status == "open"
    assert (await _select_pending(db_session, closed_id)).status == "closed"


@pytest.mark.asyncio
async def test_idempotent_when_called_twice(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    """1 度戻した行は 2 度目の sweep で対象外 (二重起動安全)。"""
    past_lease = datetime.now(UTC) - timedelta(seconds=1)
    await _make_pending(
        db_session,
        sample_source,
        url="https://example.com/sweep/idempotent",
        status="running",
        ready_at=datetime.now(UTC) - timedelta(minutes=5),
        leased_until=past_lease,
    )

    first = await sweep_expired_leases(ctx=_ctx(session_factory))
    second = await sweep_expired_leases(ctx=_ctx(session_factory))

    assert first == {"swept_count": 1}
    assert second == {"swept_count": 0}
