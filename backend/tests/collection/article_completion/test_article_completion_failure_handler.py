"""``ArticleCompletionFailureHandler`` の integration test。

検証する性質 (failure 後処理 = ``incomplete_articles`` 状態遷移 + same-tx audit):

- scrape ``Terminal`` (内容棄却) → pending ``closed`` + ``rejected`` audit
- scrape ``Retryable`` 非 exhausted → ``open`` + 未来 ready_at + ``failed`` audit
  (payload ``retry_exhausted`` 無し)
- scrape ``Retryable`` exhausted → ``closed`` + ``failed`` audit (``retry_exhausted``)
- scrape ``Retryable`` + server retry_after_seconds → その秒数で ready_at
- completion ``CompletionRejection`` → ``closed`` + ``rejected`` audit
- 失効 attempt (updated=False) → 状態変化なし + 監査に焼かない (log で観測)

handler は元の ``ScrapeFailure`` を受け内部で分類する (audit に variant を運ぶ J2)。
handler は別 session で commit するので、検証前に ``db_session`` を rollback して
fresh transaction で読む (cross-session read)。state 遷移と audit は同一 tx。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.collection.article_acquisition.repository import IncompleteArticleRepository
from app.collection.article_completion.completion_failure import CompletionRejection
from app.collection.article_completion.failure_handling import (
    ArticleCompletionFailureHandler,
)
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.article_completion.repository import ArticleCompletionRepository
from app.collection.article_completion.retry_policy import BLIP
from app.collection.article_completion.scrape_failure import ScrapeNotHtml
from app.collection.domain.analyzable_article import (
    AnalyzableArticleDefect,
    QualityTooLow,
)
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
)
from app.collection.sources.source_name import SourceName
from app.models.incomplete_article import IncompleteArticle
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result

_METRIC = "vector.completion.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


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


def _observed(source: NewsSource, url: str) -> ObservedArticle:
    return ObservedArticle(
        source_name=SourceName(str(source.name)),
        source_url=CanonicalArticleUrl(url),
        title=ObservedField(value="TC Title", origin=ObservedOrigin.feed),
        published_at=ObservedField(
            value=PublishedAt(datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)),
            origin=ObservedOrigin.feed,
        ),
    )


async def _make_ready(
    db_session: AsyncSession,
    source: NewsSource,
    url: str,
) -> ReadyForArticleCompletion:
    """``incomplete_articles`` 行を 1 件作って claim 済 Ready を返す。

    claim 後 ``status='running'`` / ``attempt_count=1``。返す Ready は
    Task 層が ``try_advance_from`` で構築するのと同じ厚い precondition 型。
    """
    incomplete_article_id = await IncompleteArticleRepository(db_session).save(
        _observed(source, url),
        source_id=source.id,
        ready_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert incomplete_article_id is not None
    await db_session.commit()
    now = datetime.now(UTC)
    repository = ArticleCompletionRepository(db_session)
    ids = await repository.claim_ready_batch(
        limit=10,
        now=now,
        leased_until=now + timedelta(minutes=5),
    )
    await db_session.commit()
    assert incomplete_article_id in ids
    return await ReadyForArticleCompletion.try_advance_from(
        incomplete_article_id=incomplete_article_id,
        repo=repository,
    )


async def _reload_pending(
    db_session: AsyncSession, incomplete_article_id: int
) -> IncompleteArticle:
    """handler の別 session commit を見るため fresh tx で pending を読み直す。"""
    await db_session.rollback()
    return (
        await db_session.execute(
            select(IncompleteArticle).where(
                IncompleteArticle.id == incomplete_article_id
            )
        )
    ).scalar_one()


async def _fetch_event(db_session: AsyncSession, source_id: int) -> PipelineEvent:
    """handler が same-tx で焼いた completion audit row を 1 件読む。"""
    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.stage == "completion",
                    PipelineEvent.source_id == source_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


async def _assert_no_event(db_session: AsyncSession, source_id: int) -> None:
    """handler が completion audit を 1 件も焼いていないことを固定する。"""
    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(
                    PipelineEvent.stage == "completion",
                    PipelineEvent.source_id == source_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_scrape_terminal_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """scrape 内容棄却 (ScrapeNotHtml) → pending closed / leased_until=None。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/term")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        ready, ScrapeNotHtml(content_type="application/pdf")
    )

    pending = await _reload_pending(db_session, ready.incomplete_article_id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_scrape_terminal_content_rejection_audits_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """内容棄却は 2 軸原則で event_type='rejected' (error_class None)。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/pdf")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        ready, ScrapeNotHtml(content_type="application/pdf")
    )

    ev = await _fetch_event(db_session, tc_source.id)
    assert ev.event_type == "rejected"
    # outcome_code = scrape_* prefix (spec のサブ段階規約)
    assert ev.outcome_code == "scrape_not_html"
    assert ev.retryability is None
    assert ev.error_class is None
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_scrape_retryable_non_exhausted_reopens_with_future_ready_at(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """scrape ``Retryable`` (502→BLIP, attempt_count=1 < max) → open + 未来 ready_at。

    BLIP.delay.minutes(1) = 0.5 分 = 30 秒。claim 直後 attempt_count=1 <
    max_attempts(8) なので exhausted ではなく retry scheduling。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/blip")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(ready, FetchGatewayError(status_code=502))

    pending = await _reload_pending(db_session, ready.incomplete_article_id)
    assert pending.status == "open"
    assert pending.leased_until is None
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=20) < delta < timedelta(seconds=40)


@pytest.mark.asyncio
async def test_scrape_retryable_non_exhausted_audits_failed_without_exhausted_flag(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """retry 中 (経路 3) は transport CODE で failed、exhausted flag は書かない。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/blip2")
    err = FetchGatewayError(status_code=502)
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(ready, err)

    ev = await _fetch_event(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == err.CODE  # transport 理由 (input 由来)
    assert ev.retryability == "retryable"
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["failure_action"] is None
    assert ev.payload["retry_exhausted"] is None  # exclude_none=False で null


@pytest.mark.asyncio
async def test_scrape_retryable_exhausted_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """scrape ``Retryable`` で attempt_count >= max_attempts → ``closed``。

    handler は DB 再読込せず ``ready.attempt_count`` を見るため、attempt_count を
    max まで UPDATE → commit → その後 Ready を構築して exhausted 判定に反映させる。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/exhaust")
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == ready.incomplete_article_id)
        .values(attempt_count=BLIP.max_attempts)
    )
    await db_session.commit()
    exhausted_ready = await ReadyForArticleCompletion.try_advance_from(
        incomplete_article_id=ready.incomplete_article_id,
        repo=ArticleCompletionRepository(db_session),
    )
    assert exhausted_ready.attempt_count == BLIP.max_attempts
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        exhausted_ready, FetchGatewayError(status_code=502)
    )

    pending = await _reload_pending(db_session, ready.incomplete_article_id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_scrape_retryable_exhausted_audits_retry_exhausted_flag(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """give-up (経路 4) は同じ transport CODE + payload ``retry_exhausted=true``。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/giveup")
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == ready.incomplete_article_id)
        .values(attempt_count=BLIP.max_attempts)
    )
    await db_session.commit()
    exhausted_ready = await ReadyForArticleCompletion.try_advance_from(
        incomplete_article_id=ready.incomplete_article_id,
        repo=ArticleCompletionRepository(db_session),
    )
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        exhausted_ready, FetchGatewayError(status_code=502)
    )

    ev = await _fetch_event(db_session, tc_source.id)
    assert ev.event_type == "failed"
    assert ev.retryability == "retryable"
    assert ev.payload["attempt_count"] == exhausted_ready.attempt_count
    assert ev.payload["failure_kind"] == "external_fetch"
    assert ev.payload["failure_action"] is None
    assert ev.payload["retry_exhausted"] is True


@pytest.mark.asyncio
async def test_scrape_retryable_uses_server_retry_after_seconds(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """scrape ``Retryable`` + retry_after_seconds=120 → ready_at が約 120 秒後。

    server 指示 (503 service_unavailable + retry_after) は policy schedule より優先。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/ra")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        ready,
        FetchOriginServerError(
            status_code=503,
            reason="service_unavailable",
            retry_after_seconds=120.0,
        ),
    )

    pending = await _reload_pending(db_session, ready.incomplete_article_id)
    assert pending.status == "open"
    assert pending.ready_at is not None
    delta = pending.ready_at - datetime.now(UTC)
    assert timedelta(seconds=100) < delta < timedelta(seconds=140)


@pytest.mark.asyncio
async def test_completion_rejected_closes_pending(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """completion ``CompletionRejection`` → pending status='closed'。

    Stage 2 拒絶は Accept 軸で retry を持たず、scrape Terminal と同様に
    pending を閉じる (別入口 / 別 log event だが状態遷移は同じ closed)。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/reject")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_completion_rejected(
        ready,
        CompletionRejection.from_quality_too_low(
            QualityTooLow(defects=(AnalyzableArticleDefect.BODY_TOO_SHORT,))
        ),
    )

    pending = await _reload_pending(db_session, ready.incomplete_article_id)
    assert pending.status == "closed"
    assert pending.leased_until is None


@pytest.mark.asyncio
async def test_completion_rejected_audits_rejected_with_defects(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """完成段棄却は rejected + 主 defect を outcome_code、全集合を payload.defects に
    焼く (経路 5)。free-text の error_class / error_message は持たない (PII-free)。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/reject2")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_completion_rejected(
        ready,
        CompletionRejection.from_quality_too_low(
            QualityTooLow(
                defects=(
                    AnalyzableArticleDefect.BODY_TOO_SHORT,
                    AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
                )
            )
        ),
    )

    ev = await _fetch_event(db_session, tc_source.id)
    assert ev.event_type == "rejected"
    assert ev.outcome_code == AnalyzableArticleDefect.BODY_TOO_SHORT
    assert ev.retryability is None
    assert ev.error_class is None
    assert ev.payload["attempt_count"] == ready.attempt_count
    assert ev.payload["defects"] == [
        AnalyzableArticleDefect.BODY_TOO_SHORT,
        AnalyzableArticleDefect.PUBLISHED_AT_MISSING,
    ]
    assert ev.payload.get("error_message") is None


@pytest.mark.asyncio
async def test_stale_attempt_records_no_audit_without_state_change(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
) -> None:
    """attempt 失効 (他 worker が attempt_count を進めた) → 監査に焼かない。

    ready は attempt_count=1 のまま、DB は別 worker が 2 に進めた状況を作る。
    close_claimed が 0 行 (updated=False) になり、stale trigger は監査に焼かず
    log で観測する (audit skip 逃がしポリシー)。pending state は触られない。
    """
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/stale")
    # 別 worker が再 claim して attempt を進めた状況 (ready.attempt_count=1 は失効)。
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == ready.incomplete_article_id)
        .values(attempt_count=ready.attempt_count + 1)
    )
    await db_session.commit()
    handler = ArticleCompletionFailureHandler(session_factory)

    with capture_logs() as logs:
        await handler.handle_scrape_failure(
            ready, ScrapeNotHtml(content_type="application/pdf")
        )

    await _assert_no_event(db_session, tc_source.id)
    pending = await _reload_pending(db_session, ready.incomplete_article_id)
    assert pending.status == "running"  # void な attempt は状態を変えない
    # 監査を外した代わりに stale trigger は escape log で観測可能に保つ。
    assert [
        e for e in logs if e.get("event") == "article_completion_stale_attempt_ignored"
    ]


# processing_outcome metric (vector.completion.processing_outcome)


def _assert_only(metrics: list, result: str) -> None:
    """``result`` だけが +1 で他値は 0 (3 値排他)。"""
    assert sum_counter_for_result(metrics, _METRIC, result) == 1
    for other in (r for r in _ALL_RESULTS if r != result):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


@pytest.mark.asyncio
async def test_scrape_retryable_emits_infra_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """一時的 transport (502→retryable) の retry → infra_error。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-retry")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(ready, FetchGatewayError(status_code=502))

    _assert_only(collected_metrics(capfire), "infra_error")


@pytest.mark.asyncio
async def test_scrape_retryable_exhausted_still_emits_infra_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """retry 上限到達でも性質は一時的 → infra_error (諦めた=failed にしない)。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-exhaust")
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == ready.incomplete_article_id)
        .values(attempt_count=BLIP.max_attempts)
    )
    await db_session.commit()
    exhausted_ready = await ReadyForArticleCompletion.try_advance_from(
        incomplete_article_id=ready.incomplete_article_id,
        repo=ArticleCompletionRepository(db_session),
    )
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        exhausted_ready, FetchGatewayError(status_code=502)
    )

    _assert_only(collected_metrics(capfire), "infra_error")


@pytest.mark.asyncio
async def test_scrape_terminal_content_emits_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """恒久的 content 失敗 (ScrapeNotHtml) → failed。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-term")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        ready, ScrapeNotHtml(content_type="application/pdf")
    )

    _assert_only(collected_metrics(capfire), "failed")


@pytest.mark.asyncio
async def test_completion_rejected_emits_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """ドメイン棄却 (CompletionRejection) → failed。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-reject")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_completion_rejected(
        ready,
        CompletionRejection.from_quality_too_low(
            QualityTooLow(defects=(AnalyzableArticleDefect.BODY_TOO_SHORT,))
        ),
    )

    _assert_only(collected_metrics(capfire), "failed")


@pytest.mark.asyncio
async def test_persist_crashed_sqlalchemy_emits_infra_error(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """persist の SQLAlchemyError → infra_error (我々の DB 障害)。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-crash")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_persist_crashed(ready, SQLAlchemyError("db lost"))

    _assert_only(collected_metrics(capfire), "infra_error")


@pytest.mark.asyncio
async def test_persist_crashed_non_db_emits_failed(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """persist の非 DB 例外 (想定外) → failed (コードバグを分母外に隠さない)。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-bug")
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_persist_crashed(ready, RuntimeError("logic bug"))

    _assert_only(collected_metrics(capfire), "failed")


@pytest.mark.asyncio
async def test_stale_attempt_does_not_emit_processing_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """claim 喪失 (updated=False) は別 worker が結末を担うため emit しない。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-stale")
    await db_session.execute(
        update(IncompleteArticle)
        .where(IncompleteArticle.id == ready.incomplete_article_id)
        .values(attempt_count=ready.attempt_count + 1)
    )
    await db_session.commit()
    handler = ArticleCompletionFailureHandler(session_factory)

    await handler.handle_scrape_failure(
        ready, ScrapeNotHtml(content_type="application/pdf")
    )

    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


@pytest.mark.asyncio
async def test_atomic_tx_commit_failure_does_not_emit_and_propagates(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """状態遷移 tx 内の DB 障害は emit 前に task へ貫通する (§5.4)。"""
    ready = await _make_ready(db_session, tc_source, "https://techcrunch.com/m-txfail")

    async def _raise(self: object, ready: object, *, now: object) -> bool:  # noqa: ARG001
        raise SQLAlchemyError("close_claimed failed")

    monkeypatch.setattr(ArticleCompletionRepository, "close_claimed", _raise)
    handler = ArticleCompletionFailureHandler(session_factory)

    with pytest.raises(SQLAlchemyError):
        await handler.handle_scrape_failure(
            ready, ScrapeNotHtml(content_type="application/pdf")
        )

    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


@pytest.mark.asyncio
async def test_persist_crashed_emits_even_when_audit_drops(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    tc_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
    capfire: CaptureLogfire,
) -> None:
    """best-effort audit が drop しても infra_error は emit される (emit 先行)。"""
    ready = await _make_ready(
        db_session, tc_source, "https://techcrunch.com/m-auditdrop"
    )

    async def _raise(self: object, *, ready: object, exc: object) -> None:  # noqa: ARG001
        raise SQLAlchemyError("audit write failed")

    monkeypatch.setattr(
        "app.audit.stages.completion."
        "ArticleCompletionAuditRepository.append_persist_crashed",
        _raise,
    )
    handler = ArticleCompletionFailureHandler(session_factory)

    # audit drop は handle_persist_crashed 内で握られ再 raise しない。
    await handler.handle_persist_crashed(ready, SQLAlchemyError("persist boom"))

    _assert_only(collected_metrics(capfire), "infra_error")
