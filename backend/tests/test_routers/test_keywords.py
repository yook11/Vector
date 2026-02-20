"""Tests for /api/v1/keywords router endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.keyword import Keyword


@pytest.mark.asyncio
class TestListKeywords:
    async def test_empty_list(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.get("/api/v1/keywords")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    async def test_returns_keywords(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.get("/api/v1/keywords")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["keyword"] == "Quantum Computing"
        assert item["category"] == "computing"
        assert item["isActive"] is True
        assert item["articleCount"] == 0

    async def test_camel_case_keys(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.get("/api/v1/keywords")
        item = resp.json()["items"][0]
        assert "isActive" in item
        assert "articleCount" in item
        assert "createdAt" in item


@pytest.mark.asyncio
class TestCreateKeyword:
    async def test_create_success(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.post(
            "/api/v1/keywords",
            json={"keyword": "Materials Informatics", "category": "materials"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["keyword"] == "Materials Informatics"
        assert data["category"] == "materials"
        assert data["isActive"] is True
        assert data["articleCount"] == 0

    async def test_create_default_category(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.post(
            "/api/v1/keywords",
            json={"keyword": "Edge AI"},
        )
        assert resp.status_code == 201
        assert resp.json()["category"] == "custom"

    async def test_create_duplicate_409(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.post(
            "/api/v1/keywords",
            json={"keyword": "Quantum Computing", "category": "computing"},
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


@pytest.mark.asyncio
class TestUpdateKeyword:
    async def test_update_is_active(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.patch(
            f"/api/v1/keywords/{sample_keyword.id}",
            json={"isActive": False},
        )
        assert resp.status_code == 200
        assert resp.json()["isActive"] is False

    async def test_update_not_found(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.patch(
            "/api/v1/keywords/99999",
            json={"isActive": False},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestDeleteKeyword:
    async def test_delete_success(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.delete(f"/api/v1/keywords/{sample_keyword.id}")
        assert resp.status_code == 204

        # Verify it's gone
        resp = await authed_client.get("/api/v1/keywords")
        assert len(resp.json()["items"]) == 0

    async def test_delete_not_found(self, authed_client: AsyncClient) -> None:
        resp = await authed_client.delete("/api/v1/keywords/99999")
        assert resp.status_code == 404
