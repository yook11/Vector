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
            "app.services.pipeline.fetch_metadata",
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
            "app.services.pipeline.fetch_metadata",
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
