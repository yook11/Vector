"""Tests for /api/v1/admin/pipeline router endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestFetchNews:
    async def test_fetch_returns_202(self, admin_client: AsyncClient) -> None:
        mock_task_handle = AsyncMock()
        mock_task_handle.task_id = "test-task-id-123"

        with patch(
            "app.tasks.collection_tasks.fetch_metadata",
        ) as mock_task:
            mock_task.kiq = AsyncMock(return_value=mock_task_handle)
            resp = await admin_client.post("/api/v1/admin/pipeline/fetch")

        assert resp.status_code == 202
        data = resp.json()
        assert data["jobId"] == "test-task-id-123"
        assert data["message"] == "Fetch task submitted"
        assert data["sourcesCount"] is None  # all due sources
        mock_task.kiq.assert_called_once_with(source_ids=None)

    async def test_fetch_with_source_ids(self, admin_client: AsyncClient) -> None:
        mock_task_handle = AsyncMock()
        mock_task_handle.task_id = "test-task-id-456"

        with patch(
            "app.tasks.collection_tasks.fetch_metadata",
        ) as mock_task:
            mock_task.kiq = AsyncMock(return_value=mock_task_handle)
            resp = await admin_client.post(
                "/api/v1/admin/pipeline/fetch",
                json={"sourceIds": [1, 2, 3]},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["sourcesCount"] == 3
        mock_task.kiq.assert_called_once_with(source_ids=[1, 2, 3])


@pytest.mark.asyncio
class TestEmbedNews:
    async def test_embed_dispatches_tasks(self, admin_client: AsyncClient) -> None:
        with (
            patch(
                "app.routers.admin.pipeline.PipelineRepository",
            ) as mock_repo_cls,
            patch(
                "app.tasks.analysis_tasks.generate_embedding",
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
                "app.tasks.analysis_tasks.generate_embedding",
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
