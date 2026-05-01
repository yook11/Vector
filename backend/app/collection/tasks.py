"""収集タスク — パイプラインの前段。

旧経路: dispatch_sources → fetch_source_metadata → fetch_content
        → analysis.tasks.extract_content
新経路: dispatch_sources → fetch_source_metadata (Strangler dispatch)
        → ingest_source (新 Protocol Fetcher で 1 段取り込み)
        → analysis.tasks.extract_content

新ルート対象ソースは ``app.collection.ingestion.strategy.NEW_ROUTE_SOURCE_NAMES``
で hardcode 管理。Phase 1c-C 完了時に旧経路を削除し、新ルート 1 本に収束させる。
"""

from __future__ import annotations

import time
from typing import assert_never

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
from app.collection.extraction.extractor import ArticleHtmlExtractor
from app.collection.extraction.service import (
    AlreadyFetchedOutcome,
    ContentFetchedOutcome,
    ContentFetchService,
    ContentFetchSkippedOutcome,
)
from app.collection.ingestion.service import (
    QuotaSkippedOutcome,
    SourceFetchedOutcome,
    SourceFetchService,
    SourceNotFoundOutcome,
)
from app.collection.ingestion.staged import StagedArticle
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
# Metadata fetch — dispatch
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
        await fetch_source_metadata.kiq(source.id)

    result = {"dispatched_count": len(sources)}
    logger.info("dispatch_sources_completed", **result)
    return result


# ---------------------------------------------------------------------------
# Metadata fetch — per source
# ---------------------------------------------------------------------------


@broker_metadata.task(
    task_name="fetch_source_metadata",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
)
async def fetch_source_metadata(
    source_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """単一ソースのメタデータを取得し、新規記事を下流キューへ dispatch する。

    Strangler 移行期: 新ルート対象 (``NEW_ROUTE_SOURCE_NAMES``) は
    ``ingest_source`` task へ振り替え、それ以外は従来通り
    ``SourceFetchService`` → ``fetch_content.kiq`` 経路で処理する。
    """
    from app.collection.ingestion.strategy import NEW_ROUTE_SOURCE_NAMES

    session_factory = ctx.state.session_factory
    async with session_factory() as session:
        source = await session.get(NewsSource, source_id)
    if source is not None and str(source.name) in NEW_ROUTE_SOURCE_NAMES:
        await ingest_source.kiq(source_id)
        logger.info(
            "fetch_source_metadata_dispatched_new_route",
            source_id=source_id,
            source=source.name,
        )
        return {"source_id": source_id, "status": "dispatched_new_route"}

    logger.info("fetch_source_metadata_started", source_id=source_id)
    svc = SourceFetchService(session_factory)
    start_time = time.monotonic()

    try:
        outcome = await svc.execute(source_id)
    except PermanentFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        return {"source_id": source_id, "status": "error", "reason": str(e)}
    except TemporaryFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
        )
        if is_last_attempt(ctx):
            logger.warning(
                "fetch_source_metadata_max_retries",
                source_id=source_id,
                error=str(e),
            )
            return {"source_id": source_id, "status": "error", "reason": str(e)}
        raise

    match outcome:
        case SourceNotFoundOutcome():
            return {"source_id": source_id, "status": "not_found"}
        case QuotaSkippedOutcome():
            return {
                "source_id": source_id,
                "status": "skipped",
                "reason": "daily_quota",
            }
        case SourceFetchedOutcome(new_discovered=discovered):
            new_count = len(discovered)
            await _record_fetch_log(
                session_factory,
                source_id,
                FetchStatus.SUCCESS,
                new_count,
                None,
                start_time,
            )
            for entity in discovered:
                await fetch_content.kiq(entity.id)
            payload = {
                "source_id": source_id,
                "new_count": new_count,
                "status": "success",
            }
            logger.info("fetch_source_metadata_completed", **payload)
            return payload
        case _:
            assert_never(outcome)


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="fetch_content",
    timeout=90,
    max_retries=3,
    retry_on_error=True,
)
async def fetch_content(
    discovered_article_id: int,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一記事の本文コンテンツを取得し Article 行を作成する。"""
    from app.analysis.extraction.domain.ready import ReadyForExtraction
    from app.analysis.extraction.repository import ExtractionRepository
    from app.analysis.tasks import extract_content

    session_factory = ctx.state.session_factory
    html_extractor = ArticleHtmlExtractor()
    svc = ContentFetchService(session_factory, html_extractor)

    try:
        outcome = await svc.execute(discovered_article_id)
    except TemporaryFetchError:
        if is_last_attempt(ctx):
            logger.warning(
                "fetch_content_max_retries",
                discovered_article_id=discovered_article_id,
            )
            return
        raise

    # Stage C へ chain (Pattern A': 上流 Task が下流 Ready を構築 — spec §7.1)
    match outcome:
        case (
            ContentFetchedOutcome(article=article)
            | AlreadyFetchedOutcome(article=article)
        ):
            async with session_factory() as session:
                extraction_repo = ExtractionRepository(session)
                ready = await ReadyForExtraction.try_advance_from(
                    article_id=article.id,
                    original_title=article.title,
                    original_content=article.body,
                    extraction_repo=extraction_repo,
                )
            if ready is not None:
                await extract_content.kiq(ready)
        case ContentFetchSkippedOutcome():
            pass  # service 側でログ済み


# ---------------------------------------------------------------------------
# New-route ingestion (Strangler 移行期)
# ---------------------------------------------------------------------------


@broker_content.task(
    task_name="ingest_source",
    timeout=300,
    max_retries=2,
    retry_on_error=True,
)
async def ingest_source(
    source_id: int,
    ctx: Context = TaskiqDepends(),
) -> dict:
    """新 Protocol Fetcher 経由でソースを 1 段で取り込む (Strangler 移行期)。

    旧経路の ``fetch_source_metadata`` → ``fetch_content`` が 2 段階で行う
    「URL 列挙 → HTML 本文取得」を、新 Protocol では Fetcher が
    ``FetchedArticle`` (本文込み) を直接返すため 1 段で完結させる。

    Stage C への chain は ``fetch_content`` と同じパターンで
    ``ReadyForExtraction.try_advance_from`` → ``extract_content.kiq``。
    """
    from app.analysis.extraction.domain.ready import ReadyForExtraction
    from app.analysis.extraction.repository import ExtractionRepository
    from app.analysis.tasks import extract_content
    from app.collection.ingestion.ingestion_service import (
        IngestedOutcome,
        IngestionService,
        SourceNotFoundOutcome,
    )
    from app.collection.ingestion.strategy import NEW_ROUTE_FETCHERS

    logger.info("ingest_source_started", source_id=source_id)
    session_factory = ctx.state.session_factory
    start_time = time.monotonic()

    async with session_factory() as session:
        source = await session.get(NewsSource, source_id)
    if source is None:
        logger.warning("ingest_source_not_found", source_id=source_id)
        return {"source_id": source_id, "status": "not_found"}

    fetcher_factory = NEW_ROUTE_FETCHERS.get(str(source.name))
    if fetcher_factory is None:
        logger.warning(
            "ingest_source_not_in_new_route",
            source_id=source_id,
            source=source.name,
        )
        return {"source_id": source_id, "status": "not_in_new_route"}

    svc = IngestionService(session_factory, fetcher_factory)

    try:
        outcome = await svc.execute(source_id)
    except PermanentFetchError as e:
        await _record_fetch_log(
            session_factory, source_id, FetchStatus.ERROR, 0, str(e), start_time
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
                error=str(e),
            )
            return {"source_id": source_id, "status": "error", "reason": str(e)}
        raise

    match outcome:
        case SourceNotFoundOutcome():
            return {"source_id": source_id, "status": "not_found"}
        case IngestedOutcome(
            persisted=articles,
            staged=staged_list,
            failed_count=fc,
            skipped_count=sc,
        ):
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
                pending: list = []
                for article in articles:
                    ready = await ReadyForExtraction.try_advance_from(
                        article_id=article.id,
                        original_title=article.title,
                        original_content=article.body,
                        extraction_repo=extraction_repo,
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
                "status": "success",
                "persisted_count": persisted_count,
                "staged_count": staged_count,
                "failed_count": fc,
                "skipped_count": sc,
            }
            logger.info("ingest_source_completed", **payload)
            return payload
        case _:
            assert_never(outcome)


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
    """Pattern H 2 段目: ``StagedArticle`` の URL から HTML を取得し本文抽出。

    エラー種別ごとの retry policy:

    - ``PermanentFetchError`` (403/404/410/451/robots/redirect/SSRF/oversize)
      → 即 drop, retry しない (再試行で結果は変わらない)
    - ``TemporaryFetchError`` (5xx/429/timeout/DNS) → ``raise`` で taskiq
      retry (``max_retries=3`` 予算内)。最終試行 (``is_last_attempt``) は drop
    - ``ExtractionEmpty`` (not_html / parse_error / quality_gate) → drop, retry なし
    - ``ReadyForArticle.try_advance_from`` が ``Failed`` (body_too_short /
      published_at_missing 等) → drop, retry なし

    成功時は merge → ``ArticleRepository.save`` で永続化 → ``ReadyForExtraction``
    を構築して ``extract_content.kiq`` に流す。
    """
    from app.analysis.extraction.domain.ready import ReadyForExtraction
    from app.analysis.extraction.repository import ExtractionRepository
    from app.analysis.tasks import extract_content
    from app.collection.extraction.domain.article import ArticleDraft
    from app.collection.extraction.extractor import (
        ArticleHtmlExtractor,
        ExtractedContent,
        ExtractionEmpty,
    )
    from app.collection.extraction.repository import ArticleRepository
    from app.collection.ingestion.domain.fetched_article import (
        Failed as IngestionFailed,
    )
    from app.collection.ingestion.domain.fetched_article import (
        ReadyForArticle,
    )

    session_factory = ctx.state.session_factory
    extractor = ArticleHtmlExtractor()
    pending = staged.pending

    try:
        html_result = await extractor.fetch(pending.source_url)
    except PermanentFetchError as e:
        logger.warning(
            "extract_html_body_permanent",
            discovered_id=staged.discovered_id,
            error=str(e),
        )
        return None
    except TemporaryFetchError:
        if is_last_attempt(ctx):
            logger.warning(
                "extract_html_body_max_retries",
                discovered_id=staged.discovered_id,
            )
            return None
        raise

    if isinstance(html_result, ExtractionEmpty):
        logger.info(
            "extract_html_body_extraction_empty",
            discovered_id=staged.discovered_id,
            reason=html_result.reason,
        )
        return None

    # 静的型上は ExtractedContent | ExtractionEmpty なので明示で narrow
    # (ExtractionEmpty は上の isinstance で先に return 済 → ここは ExtractedContent)
    if not isinstance(html_result, ExtractedContent):  # pragma: no cover
        # 防御: 型 narrowing 用の no-op (ruff S101 回避のため assert ではなく if)
        return None

    advanced = ReadyForArticle.try_advance_from(
        pending,
        body=html_result.body,
        html_published_at=html_result.published_at,
    )
    if isinstance(advanced, IngestionFailed):
        logger.info(
            "extract_html_body_quality_fail",
            discovered_id=staged.discovered_id,
            code=advanced.reason.code,
            detail=advanced.reason.detail,
        )
        return None

    fa = advanced.article
    async with session_factory() as session:
        article_repo = ArticleRepository(session)
        draft = ArticleDraft(title=fa.title, body=fa.body, published_at=fa.published_at)
        persisted = await article_repo.save(
            draft=draft,
            discovered_article_id=staged.discovered_id,
            source_id=fa.source_id,
            source_url=fa.source_url,
        )
        if persisted is None:
            persisted = await article_repo.find_by_discovered_article_id(
                staged.discovered_id
            )
        if persisted is None:
            logger.warning(
                "extract_html_body_article_persist_failed",
                discovered_id=staged.discovered_id,
            )
            await session.commit()
            return None

        extraction_repo = ExtractionRepository(session)
        ready_for_extraction = await ReadyForExtraction.try_advance_from(
            article_id=persisted.id,
            original_title=fa.title,
            original_content=fa.body,
            extraction_repo=extraction_repo,
        )
        await session.commit()

    if ready_for_extraction is not None:
        await extract_content.kiq(ready_for_extraction)
    return {
        "discovered_id": staged.discovered_id,
        "article_id": persisted.id,
        "status": "success",
    }
