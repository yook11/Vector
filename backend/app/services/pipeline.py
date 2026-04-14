"""Pipeline service — fetch and embedding backfill operations."""

from app.repositories.pipeline import PipelineRepository
from app.schemas.pipeline import (
    EmbedResponse,
    FetchResponse,
)
from app.tasks.embedding_tasks import generate_embedding
from app.tasks.metadata_tasks import fetch_metadata


class PipelineService:
    def __init__(self, repo: PipelineRepository) -> None:
        self.repo = repo

    async def backfill_embeddings(self) -> EmbedResponse:
        article_ids = await self.repo.get_article_ids_without_embedding()

        for article_id in article_ids:
            await generate_embedding.kiq(article_id)

        return EmbedResponse(
            message="Embedding tasks dispatched"
            if article_ids
            else "No articles need embedding",
            dispatched_count=len(article_ids),
        )

    @staticmethod
    async def submit_fetch(source_ids: list[int] | None) -> FetchResponse:
        task = await fetch_metadata.kiq(source_ids=source_ids)
        return FetchResponse(
            message="Fetch task submitted",
            sources_count=len(source_ids) if source_ids else None,
            job_id=task.task_id,
        )
