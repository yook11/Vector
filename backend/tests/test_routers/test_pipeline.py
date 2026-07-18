"""/api/v1/admin/pipeline ルーターエンドポイントのテスト。"""

from __future__ import annotations

import unicodedata
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.news_source import NewsSource, SourceType
from app.queue.messages.collection import AcquireSourceTaskInput

_FETCH_PATH = "/api/v1/admin/pipeline/fetch"


def _fetch_openapi_operation() -> dict[str, Any]:
    app.openapi_schema = None
    return app.openapi()["paths"][_FETCH_PATH]["post"]


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("`", "")


def _contains_any(text: str, choices: tuple[str, ...]) -> bool:
    return any(choice in text for choice in choices)


def _contains_all(text: str, terms: tuple[str, ...]) -> bool:
    return all(term in text for term in terms)


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
        """source_ids 指定時は AcquireSourceTaskInput envelope を kiq する。"""
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
            assert isinstance(arg, AcquireSourceTaskInput)
            assert arg.id in source_ids
            assert arg.name in {"VentureBeat", "TechCrunch"}

    async def test_fetch_with_source_ids_can_enqueue_an_inactive_source(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """inactive sourceの意図的な単発fetch用途を維持する。"""
        source = NewsSource(
            name="InactiveManualFetch",
            source_type=SourceType.RSS,
            site_url="https://inactive-manual-fetch.example.com",
            endpoint_url="https://inactive-manual-fetch.example.com/feed/",
            is_active=False,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        with patch("app.queue.tasks.acquisition.acquire_source") as mock_task:
            mock_task.kiq = AsyncMock()
            response = await admin_client.post(
                _FETCH_PATH,
                json={"sourceIds": [source.id]},
            )

        assert (
            response.status_code,
            response.json()["message"],
            response.json()["dispatchedCount"],
        ) == (202, "Fetch tasks submitted", 1)
        mock_task.kiq.assert_awaited_once_with(
            AcquireSourceTaskInput(id=source.id, name=str(source.name))
        )

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

    @pytest.mark.parametrize("source_id", [-1, 0, 2_147_483_648])
    async def test_fetch_rejects_source_id_outside_postgresql_integer_range(
        self,
        admin_client: AsyncClient,
        source_id: int,
    ) -> None:
        resp = await admin_client.post(
            "/api/v1/admin/pipeline/fetch",
            json={"sourceIds": [source_id]},
        )

        assert resp.status_code == 422

    @pytest.mark.parametrize("source_id", [1, 2_147_483_647])
    async def test_fetch_accepts_source_id_at_postgresql_integer_boundaries(
        self,
        admin_client: AsyncClient,
        source_id: int,
    ) -> None:
        with patch(
            "app.queue.tasks.acquisition.acquire_source",
        ) as mock_task:
            mock_task.kiq = AsyncMock()
            resp = await admin_client.post(
                "/api/v1/admin/pipeline/fetch",
                json={"sourceIds": [source_id]},
            )

        assert (resp.status_code, resp.json()["dispatchedCount"]) == (202, 0)


def test_fetch_request_openapi_declares_postgresql_integer_item_range() -> None:
    app.openapi_schema = None
    source_ids_schema = app.openapi()["components"]["schemas"]["FetchRequest"][
        "properties"
    ]["sourceIds"]
    array_schema = next(
        candidate
        for candidate in source_ids_schema["anyOf"]
        if candidate.get("type") == "array"
    )

    assert {
        "minimum": array_schema["items"].get("minimum"),
        "maximum": array_schema["items"].get("maximum"),
    } == {"minimum": 1, "maximum": 2_147_483_647}


def test_fetch_openapi_preserves_accepted_response_schema() -> None:
    operation = _fetch_openapi_operation()
    response_ref = operation["responses"]["202"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    schema = app.openapi()["components"]["schemas"]["FetchResponse"]

    assert response_ref == "#/components/schemas/FetchResponse"
    assert set(schema["properties"]) == {"message", "dispatchedCount", "jobId"}
    assert schema["required"] == ["message"]


def test_fetch_openapi_documents_the_complete_best_effort_contract() -> None:
    description = _normalized(_fetch_openapi_operation()["description"])

    acceptance_only = (
        _contains_any(description, ("best-effort", "best effort"))
        and _contains_all(
            description,
            ("202", "dispatchedcount", "enqueue", "実行", "完了", "耐久"),
        )
        and _contains_any(description, ("保証しない", "保証されない"))
    )
    inactive_manual_recovery = _contains_all(
        description,
        (
            "inactive",
            "cron",
            "operator",
            "request",
            "source id",
            "実行証跡",
            "滞留",
            "再実行",
        ),
    ) and _contains_any(
        description,
        ("自動再投入されない", "自動再投入しない", "自動では再投入されない"),
    )
    retry_cost = _contains_all(
        description,
        ("durable", "dedup", "http", "ai"),
    ) and _contains_any(description, ("再発", "再び", "繰り返"))
    partial_enqueue = _contains_all(
        description,
        ("atomic", "一部", "enqueue"),
    ) and _contains_any(description, ("非 atomic", "非atomic", "non-atomic"))
    durable_status_non_goal = _contains_all(
        description,
        ("job id", "status", "永続", "別 slice"),
    )

    assert (
        acceptance_only,
        inactive_manual_recovery,
        retry_cost,
        partial_enqueue,
        durable_status_non_goal,
    ) == (True, True, True, True, True)


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
