"""/api/v1/admin/pipeline ルーターエンドポイントのテスト。"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news_source import NewsSource, SourceType
from app.queue.messages.collection import AcquireSourceArg


@pytest.mark.asyncio
class TestFetchNews:
    async def test_fetch_without_source_ids_dispatches_all(
        self, admin_client: AsyncClient
    ) -> None:
        """source_ids 未指定時は dispatch_sources を呼ぶ。"""
        mock_task_handle = AsyncMock()
        mock_task_handle.task_id = "test-task-id-123"

        with patch(
            "app.queue.tasks.acquisition.dispatch_sources",
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
        """source_ids 指定時は AcquireSourceArg envelope を kiq する。"""
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
            "app.queue.tasks.acquisition.acquire_source",
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
            assert isinstance(arg, AcquireSourceArg)
            assert arg.id in source_ids
            assert arg.name in {"VentureBeat", "TechCrunch"}

    async def test_fetch_with_source_ids_at_max_length(
        self, admin_client: AsyncClient
    ) -> None:
        """source_ids がちょうど 100 件なら受理される (validation 境界の上限)。"""
        with patch(
            "app.queue.tasks.acquisition.acquire_source",
        ) as mock_task:
            mock_task.kiq = AsyncMock()
            resp = await admin_client.post(
                "/api/v1/admin/pipeline/fetch",
                json={"sourceIds": list(range(1, 101))},
            )

        # 該当 source_id が DB に存在しないため dispatched_count=0 だが、
        # validation 層は通って 202 を返すこと。
        assert resp.status_code == 202
        assert resp.json()["dispatchedCount"] == 0

    async def test_fetch_with_source_ids_exceeds_max_length(
        self, admin_client: AsyncClient
    ) -> None:
        """source_ids が 101 件以上なら 422 で拒否される (C4 / AUTH-N5 防御)。"""
        resp = await admin_client.post(
            "/api/v1/admin/pipeline/fetch",
            json={"sourceIds": list(range(1, 102))},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any("sourceIds" in str(d.get("loc", "")) for d in detail)


@pytest.mark.asyncio
class TestEmbedEndpointRemoved:
    """C7 (red-team) で削除した /pipeline/embed が再び route として復活
    していないことを構造的に検証する。

    旧 endpoint は body なしで `get_article_ids_without_embedding()` の
    全件 (10 万件規模) を 1 リクエストで dispatch する経済 DoS だった。
    bulk 再 embed が必要な場合は backend container 内の手動 CLI に経路を
    移譲済み (HTTP attack surface に乗らない)。
    """

    async def test_embed_route_is_not_registered(
        self, admin_client: AsyncClient
    ) -> None:
        resp = await admin_client.post("/api/v1/admin/pipeline/embed")
        # FastAPI は未登録 path に 404、method 違いに 405 を返す
        assert resp.status_code == 404
