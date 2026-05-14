"""収集タスク — パイプラインの前段。

経路: dispatch_sources → ingest_source (新 Protocol Fetcher で 1 段取り込み)
      → analysis.tasks.extract_content

全 19 ソース (RSS 18 + API 1 = Hacker News) が
``app.collection.fetchers.strategy.FETCHERS`` に登録された新 Protocol
Fetcher 経由で取り込まれる。Pattern R (RSS で本文込み) は
``ReadyForArticle`` を直接 yield → Article 永続化 + ``extract_content`` に
chain。Pattern H (RSS / API で本文未取得) は ``IncompleteArticle`` を yield
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
from app.collection.errors import SourceFetchError
from app.collection.staged import IngestSourceArg
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

    即時獲得経路 (本文込み RSS): Fetcher が ``ReadyForArticle`` を yield、
    Article 永続化 → ``ExtractionTrigger(article_id)`` で ``extract_content.kiq``
    に enqueue (案 3: Stage 3 task 側で Ready 自構築)。

    補完待ち獲得経路 (本文 HTML 必須): Fetcher が ``IncompleteArticle`` を yield、
    後段 ``extract_html_body`` task で trafilatura 抽出 + 永続化に進む。

    失敗ハンドリング: taskiq inline retry を持たず (``max_retries=0``)、
    ``SourceFetchError`` (ソース全体失敗) はその tick で監査して return する。
    次の cron tick (``dispatch_sources``) で再 dispatch されるため、Stage 2 の
    DB 駆動 retry のような state は Stage 1 では持たない。``Permanent`` /
    ``Temporary`` の細分化は Stage 2 専用語彙で、Stage 1 task 層は
    ``SourceFetchError`` 1 本で catch する。
    """
    from app.analysis.extraction.domain.ready import ExtractionTrigger
    from app.analysis.extraction.tasks import extract_content
    from app.collection.fetchers.strategy import FETCHERS
    from app.collection.service import ArticleAcquisitionService

    source_id = arg.id
    logger.info("ingest_source_started", source_id=source_id, source_name=arg.name)
    session_factory = ctx.state.session_factory
    start_time = time.monotonic()

    fetcher_factory = FETCHERS[arg.name]
    svc = ArticleAcquisitionService(session_factory, fetcher_factory)

    try:
        persisted_ids = await svc.execute(source_id)
    except SourceFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        await _record_failure_event(
            session_factory=session_factory,
            stage=Stage.SOURCE_FETCH,
            outcome_code="source_fetch_error",
            exc=e,
            attempt=1,
            duration_ms=int((time.monotonic() - start_time) * 1000),
            source_id=source_id,
        )
        return {"source_id": source_id, "status": "error", "reason": str(e)}
    except Exception as e:
        # 想定外: audit してから re-raise (worker log で可視化、taskiq retry は
        # 載らない: max_retries=0 / retry_on_error=False)
        await _record_failure_event(
            session_factory=session_factory,
            stage=Stage.SOURCE_FETCH,
            outcome_code="unexpected_error",
            exc=e,
            attempt=1,
            duration_ms=int((time.monotonic() - start_time) * 1000),
            source_id=source_id,
        )
        raise

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
    (``dispatch_html_fetch_jobs``) のみで再投入する設計。task の責務は
    戻り値 dispatch のみ:

    - ``ArticleCompletionService.execute(pending_id)`` を呼び、結果に応じて分岐
    - ``int`` (article_id) が返れば ``ExtractionTrigger(article_id)`` を
      ``extract_content.kiq`` に流す (案 3: 下流 Stage 3 task が処理開始時に
      Ready 自構築)
    - ``None`` (重複配送 / lease 衝突 / 永続失敗 / 一時失敗 / race-loss) は何
      もしない (DB 状態 + audit は Service 内で完結済、失敗詳細は
      ``pipeline_events.payload.reason_code`` で観測)
    - ``TemporaryFetchError`` は Service 内で DB 状態更新 + audit に変換される
      ため task では catch しない
    """
    from app.analysis.extraction.domain.ready import ExtractionTrigger
    from app.analysis.extraction.tasks import extract_content
    from app.collection.article_completion.service import ArticleCompletionService

    session_factory = ctx.state.session_factory
    svc = ArticleCompletionService(session_factory)
    article_id = await svc.execute(pending_id)

    if article_id is None:
        return None
    await extract_content.kiq(ExtractionTrigger(article_id=article_id))
    return {
        "pending_id": pending_id,
        "article_id": article_id,
        "status": "success",
    }
