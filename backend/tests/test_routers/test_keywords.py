"""Tests for /api/v1/keywords router endpoints."""

import pytest
from httpx import AsyncClient

from app.models.category import Category
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
        assert item["name"] == "Quantum Computing"
        assert item["articleCount"] == 0
        assert item["status"] == "provisional"

    async def test_returns_keyword_with_category(
        self,
        authed_client: AsyncClient,
        sample_keyword: Keyword,
        sample_categories: list[Category],
    ) -> None:
        resp = await authed_client.get("/api/v1/keywords")
        data = resp.json()
        item = data["items"][0]
        assert item["category"]["slug"] == "quantum"
        assert item["category"]["name"] == "量子コンピュータ"

    async def test_camel_case_keys(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.get("/api/v1/keywords")
        item = resp.json()["items"][0]
        assert "articleCount" in item
        assert "createdAt" in item
        assert "category" in item
        assert "status" in item


@pytest.mark.asyncio
class TestCreateKeyword:
    async def test_create_success(
        self, admin_client: AsyncClient, sample_categories: list[Category]
    ) -> None:
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={"name": "Materials Informatics", "categorySlug": "ai_ml"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Materials Informatics"
        assert data["category"]["slug"] == "ai_ml"
        assert data["articleCount"] == 0
        assert data["status"] == "provisional"

    async def test_create_with_invalid_category_slug(
        self, admin_client: AsyncClient, sample_categories: list[Category]
    ) -> None:
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={"name": "Bad Cat", "categorySlug": "nonexistent"},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()

    async def test_create_duplicate_409(
        self, admin_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={
                "name": "Quantum Computing",
                "categorySlug": "quantum",
            },
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


@pytest.mark.asyncio
class TestUpdateKeyword:
    async def test_update_category(
        self,
        admin_client: AsyncClient,
        sample_keyword: Keyword,
        sample_categories: list[Category],
    ) -> None:
        resp = await admin_client.patch(
            f"/api/v1/keywords/{sample_keyword.id}",
            json={"categorySlug": "semiconductor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"]["slug"] == "semiconductor"

    async def test_update_not_found(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.patch(
            "/api/v1/keywords/99999",
            json={"categorySlug": "ai_ml"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestDeleteKeyword:
    async def test_delete_success(
        self, admin_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await admin_client.delete(f"/api/v1/keywords/{sample_keyword.id}")
        assert resp.status_code == 204

        # Verify it's gone
        resp = await admin_client.get("/api/v1/keywords")
        assert len(resp.json()["items"]) == 0

    async def test_delete_not_found(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.delete("/api/v1/keywords/99999")
        assert resp.status_code == 404
