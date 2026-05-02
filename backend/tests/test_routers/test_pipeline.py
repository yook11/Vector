"""/api/v1/admin/pipeline ルーターエンドポイントのテスト。"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.ingestion.staged import IngestSourceArg
from app.models.news_source import NewsSource, SourceType


@pytest.mark.asyncio
class TestFetchNews:
    async def test_fetch_without_source_ids_dispatches_all(
        self, admin_client: AsyncClient
    ) -> None:
        """source_ids 未指定時は dispatch_sources を呼ぶ。"""
        mock_task_handle = AsyncMock()
        mock_task_handle.task_id = "test-task-id-123"

        with patch(
            "app.collection.tasks.dispatch_sources",
        ) as mock_task:
            mock_task.kiq = AsyncMock(return_value=mock_task_handle)
            resp = await admin_client.post("/api/v1/admin/pipeline/fetch")

        assert resp.status_code == 202
        data = resp.json()
        assert data["jobId"] == "test-task-id-123"
        assert data["message"] == "Dispatch task submitted"
        mock_task.kiq.assert_called_once()

    async def test_fetch_with_source_ids(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """source_ids 指定時は IngestSourceArg envelope を kiq する。"""
        sources = [
            NewsSource(
                name="VentureBeat",
                source_type=SourceType.RSS,
                site_url="https://venturebeat.com",
                endpoint_url="https://venturebeat.com/feed/",
                is_active=True,
            ),
            NewsSource(
                name="TechCrunch",
                source_type=SourceType.RSS,
                site_url="https://techcrunch.com",
                endpoint_url="https://techcrunch.com/feed/",
                is_active=True,
            ),
        ]
        for s in sources:
            db_session.add(s)
        await db_session.commit()
        for s in sources:
            await db_session.refresh(s)
        source_ids = [s.id for s in sources]

        with patch(
            "app.collection.tasks.ingest_source",
        ) as mock_task:
            mock_task.kiq = AsyncMock()
            resp = await admin_client.post(
                "/api/v1/admin/pipeline/fetch",
                json={"sourceIds": source_ids},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["dispatchedCount"] == 2
        assert data["message"] == "Fetch tasks submitted"
        assert mock_task.kiq.call_count == 2
        for call in mock_task.kiq.call_args_list:
            (arg,) = call.args
            assert isinstance(arg, IngestSourceArg)
            assert arg.id in source_ids
            assert arg.name in {"VentureBeat", "TechCrunch"}


@pytest.mark.asyncio
class TestEmbedNews:
    async def test_embed_dispatches_tasks(self, admin_client: AsyncClient) -> None:
        with (
            patch(
                "app.routers.admin.pipeline.PipelineRepository",
            ) as mock_repo_cls,
            patch(
                "app.analysis.tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_article_ids_without_embedding.return_value = [1, 2, 3]
            mock_repo_cls.return_value = mock_repo
            mock_embed.kiq = AsyncMock()

            resp = await admin_client.post("/api/v1/admin/pipeline/embed")

        assert resp.status_code == 202
        data = resp.json()
        assert data["dispatchedCount"] == 3
        assert data["message"] == "Embedding tasks dispatched"
        assert mock_embed.kiq.call_count == 3

    async def test_embed_no_articles(self, admin_client: AsyncClient) -> None:
        with (
            patch(
                "app.routers.admin.pipeline.PipelineRepository",
            ) as mock_repo_cls,
            patch(
                "app.analysis.tasks.generate_embedding",
            ) as mock_embed,
        ):
            mock_repo = AsyncMock()
            mock_repo.get_article_ids_without_embedding.return_value = []
            mock_repo_cls.return_value = mock_repo
            mock_embed.kiq = AsyncMock()

            resp = await admin_client.post("/api/v1/admin/pipeline/embed")

        assert resp.status_code == 202
        data = resp.json()
        assert data["dispatchedCount"] == 0
        assert data["message"] == "No articles need embedding"
        mock_embed.kiq.assert_not_called()
