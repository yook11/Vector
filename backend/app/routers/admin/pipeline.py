"""パイプライン操作（fetch, embed）用の管理者エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies import get_session
from app.models.news_source import NewsSource
from app.repositories.pipeline import PipelineRepository
from app.schemas.pipeline import (
    EmbedResponse,
    FetchRequest,
    FetchResponse,
)

router = APIRouter(prefix="/pipeline", tags=["admin:pipeline"])


@router.post(
    "/fetch",
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    session: Annotated[AsyncSession, Depends(get_session)],
    body: FetchRequest | None = None,
) -> FetchResponse:
    """ニュース取得タスクをキュー投入する。

    source_ids 指定時はソースごとに個別タスクを dispatch、
    未指定時は dispatch_sources で全アクティブソースを dispatch。
    """
    from app.collection.ingestion.staged import IngestSourceArg
    from app.collection.tasks import dispatch_sources, ingest_source

    source_ids = body.source_ids if body else None

    if source_ids:
        # ingest_source は IngestSourceArg(id, name) envelope を要求するため、
        # 指定された source_id 群の name を 1 度の query で解決する。
        result = await session.execute(
            select(NewsSource.id, NewsSource.name).where(
                NewsSource.id.in_(source_ids)  # type: ignore[attr-defined]
            )
        )
        name_by_id: dict[int, str] = {row.id: str(row.name) for row in result}
        for sid in source_ids:
            name = name_by_id.get(sid)
            if name is None:
                continue
            await ingest_source.kiq(IngestSourceArg(id=sid, name=name))
        return FetchResponse(
            message="Fetch tasks submitted",
            dispatched_count=sum(1 for sid in source_ids if sid in name_by_id),
        )

    task = await dispatch_sources.kiq()
    return FetchResponse(
        message="Dispatch task submitted",
        job_id=task.task_id,
    )


@router.post(
    "/embed",
    status_code=status.HTTP_202_ACCEPTED,
    summary="埋め込み未生成の分析に対して埋め込みタスクをディスパッチする",
)
async def embed_news(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EmbedResponse:
    """埋め込み未生成の全記事に対して generate_embedding タスクを投入する。"""
    from app.analysis.tasks import generate_embedding

    repo = PipelineRepository(session)
    article_ids = await repo.get_article_ids_without_embedding()
    for article_id in article_ids:
        await generate_embedding.kiq(article_id)
    return EmbedResponse(
        message="Embedding tasks dispatched"
        if article_ids
        else "No articles need embedding",
        dispatched_count=len(article_ids),
    )
