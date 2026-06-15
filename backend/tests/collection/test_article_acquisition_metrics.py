"""``vector.acquisition.outcome`` / ``vector.acquisition.run`` metric の oracle テスト。

検証する性質:
- ``ArticleAcquisitionService.execute`` が commit 後に entry 変換結末を
  ``vector.acquisition.outcome{result=<analyzable|observed|rejected>}`` counter に
  正しく加算する (commit 前に raise した場合は emit しない)。
- dedup skip (同 URL 既存で continue) は計数しない。
- ``acquire_source`` task が run 結末を
  ``vector.acquisition.run{result=<succeeded|failed>}`` counter に +1 する。
- attribute 経路に PII (article_id / URL 様の dynamic 値) が混入しない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_acquisition.errors import AcquisitionReadError
from app.collection.article_acquisition.fetched_article import FetchedArticle
from app.collection.article_acquisition.service import ArticleAcquisitionService
from app.collection.article_acquisition.tools.reader_tools import ReaderTools
from app.collection.domain.observed_article import ObservedOrigin
from app.collection.external_fetch_errors import FetchSsrfBlockedError
from app.collection.sources.article_completion_policy import (
    DEFAULT_POLICY,
    ArticleCompletionPolicy,
)
from app.collection.sources.base_article_source import BaseArticleSource
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName
from app.models.news_source import NewsSource, SourceType
from app.queue.messages.collection import AcquireSourceTaskInput
from app.queue.tasks import acquisition as collection_tasks

_PUBLISHED = datetime(2026, 4, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# FetchedArticle builders (パターン別)
# ---------------------------------------------------------------------------


def _ready_fetched(url: str) -> FetchedArticle:
    """analyzable 経路: body + title + published 全て揃い。"""
    return FetchedArticle(
        title="Test Title", url=url, body="x" * 100, published_at=_PUBLISHED
    )


def _pending_fetched(url: str) -> FetchedArticle:
    """observed 経路: body=None で Ready 不成立。"""
    return FetchedArticle(title="TC Title", url=url, body=None, published_at=_PUBLISHED)


def _rejection_fetched(url: str = "https://venturebeat.com/x") -> FetchedArticle:
    """rejected 経路: title が空白文字のみで MISSING_TITLE 棄却になる。"""
    return FetchedArticle(title="   ", url=url, body="x" * 42, published_at=None)


# ---------------------------------------------------------------------------
# Stub source (最小定義)
# ---------------------------------------------------------------------------


class _StubSource(BaseArticleSource):
    """FetchedArticle を直接注入する ArticleSource 構造的 fake。"""

    name: ClassVar[SourceName] = SourceName("VentureBeat")
    endpoint_url: ClassVar[str] = "https://venturebeat.com/feed/"
    observed_origin: ClassVar[ObservedOrigin] = ObservedOrigin.feed
    completion_policy: ClassVar[ArticleCompletionPolicy] = DEFAULT_POLICY
    fetch_cadence: ClassVar[FetchCadence] = FetchCadence.HIGH

    def __init__(self, items: list[FetchedArticle]) -> None:
        self._items = items

    async def read(self, tools: ReaderTools) -> list[FetchedArticle]:  # noqa: ARG002
        return self._items

    def map_entry(self, entry: FetchedArticle) -> FetchedArticle:
        return entry


# ---------------------------------------------------------------------------
# capfire helpers (test_curation_hold_metrics.py と同パターン)
# ---------------------------------------------------------------------------


def _find_metric(metrics: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((m for m in metrics if m["name"] == name), None)


def _sum_value(metric: dict[str, Any]) -> int:
    return sum(int(dp["value"]) for dp in metric["data"]["data_points"])


def _attributes_for(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [dp.get("attributes", {}) for dp in metric["data"]["data_points"]]


def _sum_for_result(metrics: list[dict[str, Any]], name: str, result: str) -> int:
    """指定 metric の ``result`` attribute が一致する data_point の合計値。"""
    m = _find_metric(metrics, name)
    if m is None:
        return 0
    return sum(
        int(dp["value"])
        for dp in m["data"]["data_points"]
        if dp.get("attributes", {}).get("result") == result
    )


# ---------------------------------------------------------------------------
# vb_source fixture (test_article_acquisition_service.py と同パターン)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# task helper (test_acquire_source_task_audit.py と同パターン)
# ---------------------------------------------------------------------------


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    state = SimpleNamespace(session_factory=session_factory)
    message = SimpleNamespace(labels={})
    return SimpleNamespace(state=state, message=message)


# ---------------------------------------------------------------------------
# entry counter テスト (service.execute 経由)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_analyzable_only_emits_analyzable_sum1(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """analyzable 1 件のみ投入 → outcome{result=analyzable} sum==1、他は不在 or 0。"""
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_ready_fetched("https://venturebeat.com/a1/")]),
    )

    await svc.execute(vb_source.id)

    metrics = capfire.get_collected_metrics()
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "analyzable") == 1
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "observed") == 0
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "rejected") == 0


@pytest.mark.asyncio
async def test_outcome_observed_only_emits_observed_sum1(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """observed 1 件のみ (body=None) → outcome{result=observed} sum==1。"""
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_pending_fetched("https://techcrunch.com/h1/")]),
    )

    await svc.execute(vb_source.id)

    metrics = capfire.get_collected_metrics()
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "observed") == 1
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "analyzable") == 0
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "rejected") == 0


@pytest.mark.asyncio
async def test_outcome_rejected_only_emits_rejected_sum1(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """rejected 1 件のみ (title 空白) → outcome{result=rejected} sum==1。"""
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_rejection_fetched()]),
    )

    await svc.execute(vb_source.id)

    metrics = capfire.get_collected_metrics()
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "rejected") == 1
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "analyzable") == 0
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "observed") == 0


@pytest.mark.asyncio
async def test_outcome_mixed_emits_each_result_sum1(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """analyzable+observed+rejected 混在 → 3 result それぞれ sum==1。

    per-result 帰属が正しいことを区別する非空虚ケース。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/ok/"),
                _pending_fetched("https://techcrunch.com/h1/"),
                _rejection_fetched(),
            ]
        ),
    )

    await svc.execute(vb_source.id)

    metrics = capfire.get_collected_metrics()
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "analyzable") == 1
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "observed") == 1
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "rejected") == 1


@pytest.mark.asyncio
async def test_outcome_dedup_url_counts_analyzable_only_once(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """同一 analyzable URL を 2 回 yield → outcome{result=analyzable} sum==1。

    2 度目は ON CONFLICT DO NOTHING で save が None を返し、service は continue する
    (dedup skip は非計数)。「entry ごとに naive に +1」する実装を落とす非空虚オラクル。
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

    metrics = capfire.get_collected_metrics()
    # 2 件 yield しても dedup skip により analyzable は 1 のみ。
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "analyzable") == 1


@pytest.mark.asyncio
async def test_outcome_attribute_keys_are_result_only_no_pii(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """outcome metric の全 data_point attribute keys が {"result"} のみ、PII 不在。

    dump 全体に article_id / URL 様の dynamic 値が混入しない構造的契約を
    full-text oracle で検知。
    """
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource([_ready_fetched("https://venturebeat.com/pii-check/")]),
    )

    await svc.execute(vb_source.id)

    metrics = capfire.get_collected_metrics()
    outcome = _find_metric(metrics, "vector.acquisition.outcome")
    assert outcome is not None, "vector.acquisition.outcome が exporter に届かない"
    for attrs in _attributes_for(outcome):
        assert set(attrs.keys()) == {"result"}, (
            f"outcome attribute に予期しない key: {attrs.keys()}"
        )

    dumped = json.dumps(metrics, default=str, ensure_ascii=False)
    forbidden_substrings = ("article_id", "http://", "https://", "source_id")
    for needle in forbidden_substrings:
        assert needle not in dumped, (
            f"acquisition outcome metric dump に PII 様文字列 {needle!r} が混入"
        )


class _FailingConversionAuditRepo:
    """append_conversion_rejected が必ず raise する監査リポジトリ stub。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def append_conversion_rejected(self, **__: Any) -> None:
        raise RuntimeError("conversion audit insert boom")


@pytest.mark.asyncio
async def test_outcome_rejected_not_emitted_when_audit_dropped(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """rejected の best-effort 監査が drop したら outcome{rejected} を計上しない。

    ``handle_conversion_rejected`` の監査 commit が失敗すると pipeline_events 行は
    残らない。metric を監査に揃えるため、この場合 rejected counter は increment
    しない (監査成否に関係なく +1 する旧実装を落とす非空虚オラクル)。analyzable 1 件を
    併投入し metric dump を非空にする (capfire は zero-metric で crash するため)。
    """
    monkeypatch.setattr(
        "app.collection.article_acquisition.failure_handling."
        "SourceAcquisitionAuditRepository",
        _FailingConversionAuditRepo,
    )
    svc = ArticleAcquisitionService(
        session_factory,
        _StubSource(
            [
                _ready_fetched("https://venturebeat.com/ok/"),
                _rejection_fetched(),
            ]
        ),
    )

    await svc.execute(vb_source.id)

    metrics = capfire.get_collected_metrics()
    # analyzable は通常どおり計上され (stream は止まらない)、rejected は監査 drop で 0。
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "analyzable") == 1
    assert _sum_for_result(metrics, "vector.acquisition.outcome", "rejected") == 0


# ---------------------------------------------------------------------------
# run counter テスト (acquire_source task 経由)
# ---------------------------------------------------------------------------


class _SucceedingService:
    """execute が空リストを返す ArticleAcquisitionService スタブ。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def execute(self, source_id: int) -> list[int]:  # noqa: ARG002
        return []


class _RaisingService:
    """execute が AcquisitionReadError を raise する service スタブ。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def execute(self, source_id: int) -> Any:  # noqa: ARG002
        raise AcquisitionReadError(
            origin=FetchSsrfBlockedError("ssrf blocked: 10.0.0.1")
        )


@pytest.mark.asyncio
async def test_run_succeeded_emits_succeeded_sum1(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """execute が [] を返す stub → run{result=succeeded} sum==1、failed は不在 or 0。

    空リストなら downstream curate_content.kiq は呼ばれないので broker 不要。
    """
    monkeypatch.setattr(
        "app.collection.article_acquisition.service.ArticleAcquisitionService",
        _SucceedingService,
    )
    ctx = _ctx(session_factory)

    await collection_tasks.acquire_source(
        AcquireSourceTaskInput(id=vb_source.id, name="VentureBeat"),
        ctx=ctx,  # type: ignore[arg-type]
    )

    metrics = capfire.get_collected_metrics()
    assert _sum_for_result(metrics, "vector.acquisition.run", "succeeded") == 1
    assert _sum_for_result(metrics, "vector.acquisition.run", "failed") == 0


@pytest.mark.asyncio
async def test_run_failed_emits_failed_sum1(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """execute が AcquisitionReadError を raise する stub → run{result=failed} sum==1。

    AcquisitionReadError は AcquisitionError なので handle_source_failure が
    reraise=False を返し task は return する。succeeded は不在 or 0。
    """
    monkeypatch.setattr(
        "app.collection.article_acquisition.service.ArticleAcquisitionService",
        _RaisingService,
    )
    ctx = _ctx(session_factory)

    result = await collection_tasks.acquire_source(
        AcquireSourceTaskInput(id=vb_source.id, name="VentureBeat"),
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    metrics = capfire.get_collected_metrics()
    assert _sum_for_result(metrics, "vector.acquisition.run", "failed") == 1
    assert _sum_for_result(metrics, "vector.acquisition.run", "succeeded") == 0
