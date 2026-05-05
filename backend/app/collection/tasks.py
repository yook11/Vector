"""収集タスク — パイプラインの前段。

経路: dispatch_sources → ingest_source (新 Protocol Fetcher で 1 段取り込み)
      → analysis.tasks.extract_content

全 19 ソース (RSS 18 + API 1 = Hacker News) が
``app.collection.ingestion.strategy.FETCHERS`` に登録された新 Protocol
Fetcher 経由で取り込まれる。Pattern R (RSS で本文込み) は
``ReadyForArticle`` を直接 yield → Article 永続化 + ``extract_content`` に
chain。Pattern H (RSS / API で本文未取得) は ``PendingHtmlFetch`` を yield
→ ``extract_html_body`` task が HTML 取得 + trafilatura 抽出 + 永続化に進む。
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select
from taskiq import Context, TaskiqDepends

from app.brokers import (
    _FETCH_CRON,
    broker_content,
    broker_metadata,
    is_last_attempt,
)
from app.collection.errors import (
    PermanentFetchError,
    TemporaryFetchError,
)
from app.collection.ingestion.staged import IngestSourceArg, StagedArticle
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_source import NewsSource
from app.observability.domain.event import Stage
from app.observability.recording import _record_failure_event

logger = structlog.get_logger(__name__)


async def _record_fetch_log(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: int,
    status: FetchStatus,
    articles_count: int,
    error_message: str | None,
    start_time: float,
) -> None:
    """単一 FetchLog 行を書き込む。Task 層の「実行結果記録」責務。"""
    duration_ms = int((time.monotonic() - start_time) * 1000)
    async with session_factory() as session:
        session.add(
            FetchLog(
                source_id=source_id,
                status=status,
                articles_count=articles_count,
                error_message=error_message,
                duration_ms=duration_ms,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Dispatch — cron 起動、全アクティブソースを ingest_source に投げる
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="dispatch_sources",
    timeout=60,
    max_retries=1,
    retry_on_error=True,
    schedule=[{"cron": _FETCH_CRON}],
)
async def dispatch_sources(
    ctx: Context = TaskiqDepends(),
) -> dict:
    """全アクティブソースを走査し、ソースごとに個別タスクを dispatch する。"""
    logger.info("dispatch_sources_started")
    session_factory = ctx.state.session_factory

    async with session_factory() as session:
        sources = list(
            (
                await session.execute(
                    select(NewsSource)
                    .where(NewsSource.is_active == True)  # noqa: E712
                    .order_by(NewsSource.name)
                )
            )
            .scalars()
            .all()
        )

    if not sources:
        logger.info("dispatch_sources_skipped", reason="no active sources")
        return {"dispatched_count": 0}

    for source in sources:
        await ingest_source.kiq(IngestSourceArg(id=source.id, name=str(source.name)))

    result = {"dispatched_count": len(sources)}
    logger.info("dispatch_sources_completed", **result)
    return result


# ---------------------------------------------------------------------------
# Ingest — per-source の取り込み (新 Protocol Fetcher で 1 段で完結)
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="ingest_source",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
)
async def ingest_source(
    arg: IngestSourceArg,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """新 Protocol Fetcher 経由でソースを取り込む。

    ``arg.id`` は ``news_sources.id`` (FK 用)、``arg.name`` は ``FETCHERS``
    dispatch dict の lookup キー。``dispatch_sources`` で 1 度だけ DB を
    引いて envelope に詰めているため、本 task では ``NewsSource`` を再
    lookup しない。

    Pattern R (本文込み RSS): Fetcher が ``ReadyForArticle`` を yield、
    Article 永続化 → ``ReadyForExtraction`` 構築 → ``extract_content.kiq``。

    Pattern H (本文 HTML 必須): Fetcher が ``PendingHtmlFetch`` を yield、
    後段 ``extract_html_body`` task で trafilatura 抽出 + 永続化に進む。
    """
    from app.analysis.extraction.domain.ready import ReadyForExtraction
    from app.analysis.extraction.noise_repository import NoiseRepository
    from app.analysis.extraction.repository import ExtractionRepository
    from app.analysis.tasks import extract_content
    from app.collection.ingestion.ingestion_service import IngestionService
    from app.collection.ingestion.strategy import FETCHERS

    source_id = arg.id
    logger.info("ingest_source_started", source_id=source_id, source_name=arg.name)
    session_factory = ctx.state.session_factory
    start_time = time.monotonic()
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1

    fetcher_factory = FETCHERS[arg.name]
    svc = IngestionService(session_factory, fetcher_factory)

    try:
        outcome = await svc.execute(source_id, attempt=attempt)
    except PermanentFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        await _record_failure_event(
            session_factory=session_factory,
            stage=Stage.SOURCE_FETCH,
            outcome_code="permanent_fetch_error",
            exc=e,
            attempt=attempt,
            duration_ms=int((time.monotonic() - start_time) * 1000),
            source_id=source_id,
        )
        return {"source_id": source_id, "status": "error", "reason": str(e)}
    except TemporaryFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        if is_last_attempt(ctx):
            logger.warning(
                "ingest_source_max_retries",
                source_id=source_id,
                source_name=arg.name,
                error=str(e),
            )
            await _record_failure_event(
                session_factory=session_factory,
                stage=Stage.SOURCE_FETCH,
                outcome_code="temporary_fetch_error_exhausted",
                exc=e,
                attempt=attempt,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                source_id=source_id,
            )
            return {"source_id": source_id, "status": "error", "reason": str(e)}
        raise
    except Exception as e:
        # 想定外: audit してから re-raise (taskiq の standard retry に乗せる)
        await _record_failure_event(
            session_factory=session_factory,
            stage=Stage.SOURCE_FETCH,
            outcome_code="unexpected_error",
            exc=e,
            attempt=attempt,
            duration_ms=int((time.monotonic() - start_time) * 1000),
            source_id=source_id,
        )
        raise

    articles = outcome.persisted
    staged_list = outcome.staged
    persisted_count = len(articles)
    staged_count = len(staged_list)
    await _record_fetch_log(
        session_factory,
        source_id,
        FetchStatus.SUCCESS,
        persisted_count,
        None,
        start_time,
    )
    # Pattern R 経路: 永続化済 Article から ReadyForExtraction を構築し kiq
    async with session_factory() as session:
        extraction_repo = ExtractionRepository(session)
        noise_repo = NoiseRepository(session)
        pending: list = []
        for article in articles:
            ready = await ReadyForExtraction.try_advance_from(
                article_id=article.id,
                original_title=article.title,
                original_content=article.body,
                extraction_repo=extraction_repo,
                noise_repo=noise_repo,
            )
            if ready is not None:
                pending.append(ready)
    for ready in pending:
        await extract_content.kiq(ready)
    # Pattern H 経路: discovered のみ作成済の StagedArticle を 2 段目 task へ
    for staged in staged_list:
        await extract_html_body.kiq(staged)
    payload = {
        "source_id": source_id,
        "source_name": arg.name,
        "status": "success",
        "persisted_count": persisted_count,
        "staged_count": staged_count,
    }
    # failed_count / skipped_count は pipeline_events.payload で確認 (Outcome 純化)
    logger.info("ingest_source_completed", **payload)
    return payload


# ---------------------------------------------------------------------------
# Pattern H: 2 段目 — HTML 取得 + 本文抽出 + Article 永続化
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="extract_html_body",
    timeout=60,
    max_retries=3,
    retry_on_error=True,
)
async def extract_html_body(
    staged: StagedArticle,
    ctx: Context = TaskiqDepends(),
) -> dict | None:
    """Pattern H 2 段目: HTML 取得 + 本文抽出 + Article 永続化を Service に委譲。

    task の責務は **taskiq retry policy** + **Outcome dispatch** のみ:

    - ``ContentFetchService.execute`` を呼び、結果に応じて分岐
    - ``TemporaryFetchError`` は raise で taskiq retry に乗せる
      (``max_retries=3`` 予算内)。最終試行 (``is_last_attempt``) は
      ``svc.audit_exhausted`` を呼んで drop
    - ``ContentFetched`` 時のみ ``ReadyForExtraction`` を構築し
      ``extract_content.kiq`` に流す
    - ``TerminallyDropped`` / ``TransientlyDropped`` は何もしない
      (audit は Service 内で焼き付け済)
    """
    from app.analysis.extraction.domain.ready import ReadyForExtraction
    from app.analysis.extraction.noise_repository import NoiseRepository
    from app.analysis.extraction.repository import ExtractionRepository
    from app.analysis.tasks import extract_content
    from app.collection.extraction.content_fetch_service import (
        ContentFetched,
        ContentFetchService,
        TerminallyDropped,
        TransientlyDropped,
    )

    session_factory = ctx.state.session_factory
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1
    svc = ContentFetchService(session_factory)

    try:
        outcome = await svc.execute(staged, attempt=attempt)
    except TemporaryFetchError as e:
        if is_last_attempt(ctx):
            await svc.audit_exhausted(staged, attempt=attempt, exc=e)
            logger.warning(
                "extract_html_body_max_retries",
                discovered_id=staged.discovered_id,
            )
            return None
        raise

    match outcome:
        case ContentFetched(article=article):
            async with session_factory() as session:
                ready_for_extraction = await ReadyForExtraction.try_advance_from(
                    article_id=article.id,
                    original_title=article.title,
                    original_content=article.body,
                    extraction_repo=ExtractionRepository(session),
                    noise_repo=NoiseRepository(session),
                )
            if ready_for_extraction is not None:
                await extract_content.kiq(ready_for_extraction)
            return {
                "discovered_id": staged.discovered_id,
                "article_id": article.id,
                "status": "success",
            }
        case TerminallyDropped() | TransientlyDropped():
            return None
