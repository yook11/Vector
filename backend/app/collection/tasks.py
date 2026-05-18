"""収集タスク — パイプラインの前段。

経路: dispatch_sources → ingest_source (新 Protocol Fetcher で 1 段取り込み)
      → analysis.tasks.extract_content

全 19 ソース (RSS 18 + API 1 = Hacker News) が
``app.collection.source_fetch.strategy.FETCHERS`` に登録された新 Protocol
Fetcher 経由で取り込まれる。Pattern R (RSS で本文込み) は
``AnalyzableArticle`` を直接 yield → Article 永続化 + ``extract_content`` に
chain。Pattern H (RSS / API で本文未取得) は ``ObservedArticle`` を yield
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
)
from app.collection.source_fetch.failure_handling import SourceFetchFailureHandler
from app.collection.staged import IngestSourceArg
from app.models.fetch_log import FetchLog, FetchStatus
from app.models.news_source import NewsSource

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
    max_retries=0,
    retry_on_error=False,
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

    即時獲得経路 (本文込み RSS): Fetcher が ``AnalyzableArticle`` を yield、
    Article 永続化 → ``ExtractionTrigger(article_id)`` で ``extract_content.kiq``
    に enqueue (案 3: Stage 3 task 側で Ready 自構築)。

    補完待ち獲得経路 (本文 HTML 必須): Fetcher が ``ObservedArticle`` を yield、
    後段 ``extract_html_body`` task で trafilatura 抽出 + 永続化に進む。

    失敗ハンドリング: taskiq inline retry を持たず (``max_retries=0``)、捕捉した
    例外は ``SourceFetchFailureHandler`` に委譲する。``SourceFetchError`` (ソース
    全体失敗) は origin CODE つきで監査して return、想定外例外は監査の上 re-raise
    (worker log で可視化)。次の cron tick (``dispatch_sources``) で再 dispatch
    されるため、Stage 1 は Stage 2 のような DB 駆動 retry state を持たない。
    """
    from app.analysis.extraction.domain.ready import ExtractionTrigger
    from app.analysis.extraction.tasks import extract_content
    from app.collection.source_fetch.service import ArticleAcquisitionService
    from app.collection.source_fetch.strategy import FETCHERS

    source_id = arg.id
    logger.info("ingest_source_started", source_id=source_id, source_name=arg.name)
    session_factory = ctx.state.session_factory
    start_time = time.monotonic()

    fetcher_factory = FETCHERS[arg.name]
    svc = ArticleAcquisitionService(session_factory, fetcher_factory)

    handler = SourceFetchFailureHandler(session_factory)
    try:
        persisted_ids = await svc.execute(source_id)
    except Exception as exc:
        # FetchLog (実行結果記録) は Task 層の別責務として従来どおり書く。
        # marker 分類 → audit / reraise 判断は handler に一本化する。
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(exc), start_time
        )
        reraise = await handler.handle(
            source_id=source_id,
            source_name=arg.name,
            exc=exc,
            attempt=1,
        )
        if reraise:
            raise
        return {"source_id": source_id, "status": "error", "reason": str(exc)}

    article_created_count = len(persisted_ids)
    await _record_fetch_log(
        session_factory,
        source_id,
        FetchStatus.SUCCESS,
        article_created_count,
        None,
        start_time,
    )
    # Pattern R 経路: 永続化済 article_id を ID-only Trigger に詰めて kiq
    # (案 3: precondition 判定 + Ready 構築は下流 Stage 3 task が処理開始時)
    for article_id in persisted_ids:
        await extract_content.kiq(ExtractionTrigger(article_id=article_id))
    # Pattern H 経路: PR2.5-B cutover で `pending_html_articles` の DB 駆動に移行。
    # `dispatch_html_fetch_jobs` cron poller が `pending_id` を `extract_html_body`
    # に投入するため、ここでの直接 kiq は撤去 (Block 3 で cron poller 新設予定)。
    payload = {
        "source_id": source_id,
        "source_name": arg.name,
        "status": "success",
        "article_created_count": article_created_count,
    }
    logger.info("ingest_source_completed", **payload)
    return payload


# ---------------------------------------------------------------------------
# Pattern H: 2 段目 — HTML 取得 + 本文抽出 + Article 永続化
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="extract_html_body",
    timeout=60,
    max_retries=0,
    retry_on_error=False,
)
async def extract_html_body(
    pending_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict | None:
    """Pattern H 2 段目: HTML 取得 + 本文抽出 + Article 永続化を Service に委譲。

    PR2.5-B cutover で taskiq retry を完全に殺し、cron poller
    (``dispatch_html_fetch_jobs``) のみで再投入する設計。案 3 cutover で Stage
    3/4 と同型化: task が処理開始時に ``ReadyForArticleCompletion.try_advance_from``
    で厚い Ready を自構築し (precondition ``status='running'`` 未充足なら skip
    log + ``None``)、Service は Ready だけ受け取る。task の責務:

    - ``ReadyForArticleCompletion.try_advance_from(pending_id)`` で Ready 構築。
      ``None`` (重複配送 / lease 衝突 / sweep 済 / close 済) は skip log + ``None``
    - ``ArticleCompletionService.execute(ready)`` を呼び、結果に応じて分岐
    - ``int`` (article_id) が返れば ``ExtractionTrigger(article_id)`` を
      ``extract_content.kiq`` に流す (下流 Stage 3 task が処理開始時に Ready 自構築)
    - ``None`` (永続失敗 / 一時失敗 / race-loss) は何もしない (DB 状態 + audit は
      Service / failure handler 内で完結済、失敗詳細は構造化ログで観測)
    - ``TemporaryFetchError`` は Service 内で DB 状態更新 + audit に変換される
      ため task では catch しない
    """
    from app.analysis.extraction.domain.ready import ExtractionTrigger
    from app.analysis.extraction.tasks import extract_content
    from app.collection.article_completion.ready import ReadyForArticleCompletion
    from app.collection.article_completion.repository import (
        ArticleCompletionRepository,
    )
    from app.collection.article_completion.service import ArticleCompletionService

    session_factory = ctx.state.session_factory
    async with session_factory() as session:
        ready = await ReadyForArticleCompletion.try_advance_from(
            pending_id=pending_id,
            repo=ArticleCompletionRepository(session),
        )
    if ready is None:
        logger.info(
            "extract_html_body_skipped",
            pending_id=pending_id,
            reason="precondition_not_met",
        )
        return None

    article_id = await ArticleCompletionService(session_factory).execute(ready)

    if article_id is None:
        return None
    await extract_content.kiq(ExtractionTrigger(article_id=article_id))
    return {
        "pending_id": pending_id,
        "article_id": article_id,
        "status": "success",
    }
