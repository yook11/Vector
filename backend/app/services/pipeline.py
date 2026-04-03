"""Pipeline service — fetch and embedding backfill operations."""

from app.repositories.pipeline import PipelineRepository
from app.schemas.pipeline import (
    EmbedResponse,
    FetchResponse,
)
from app.services.embedding import embed_articles
from app.tasks.pipeline_tasks import fetch_metadata


class PipelineService:
    def __init__(self, repo: PipelineRepository) -> None:
        self.repo = repo

    async def backfill_embeddings(self) -> EmbedResponse:
        analyses = await self.repo.get_analyses_without_embedding()

        if not analyses:
            return EmbedResponse(
                message="No analyses need embedding",
                embedded_count=0,
                skipped_count=0,
                error_count=0,
            )

        er = await embed_articles(self.repo.session, analyses)

        return EmbedResponse(
            message=f"Embedding completed: {er.embedded_count} embedded, "
            f"{er.error_count} errors",
            embedded_count=er.embedded_count,
            skipped_count=er.skipped_count,
            error_count=er.error_count,
        )

    @staticmethod
    async def submit_fetch(source_ids: list[int] | None) -> FetchResponse:
        task = await fetch_metadata.kiq(source_ids=source_ids)
        return FetchResponse(
            message="Fetch task submitted",
            sources_count=len(source_ids) if source_ids else None,
            job_id=task.task_id,
        )
