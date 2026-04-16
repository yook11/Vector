"""パイプライン操作（fetch, embed）用の管理者エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.pipeline import PipelineRepository
from app.schemas.pipeline import (
    EmbedResponse,
    FetchRequest,
    FetchResponse,
)

router = APIRouter(prefix="/pipeline", tags=["admin:pipeline"])


@router.post(
    "/fetch",
    response_model=FetchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fetch_news(
    body: FetchRequest | None = None,
) -> FetchResponse:
    """ニュース取得タスクをキュー投入し、タスク ID を即座に返す。"""
    from app.tasks.collection_tasks import fetch_metadata

    source_ids = body.source_ids if body else None
    task = await fetch_metadata.kiq(source_ids=source_ids)
    return FetchResponse(
        message="Fetch task submitted",
        sources_count=len(source_ids) if source_ids else None,
        job_id=task.task_id,
    )


@router.post(
    "/embed",
    response_model=EmbedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="埋め込み未生成の分析に対して埋め込みタスクをディスパッチする",
)
async def embed_news(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EmbedResponse:
    """埋め込み未生成の全記事に対して generate_embedding タスクを投入する。"""
    from app.tasks.analysis_tasks import generate_embedding

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
