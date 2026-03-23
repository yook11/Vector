"""Tests for /api/v1/keywords router endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category, KeywordCategoryLink
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
        assert item["articleCount"] == 0

    async def test_returns_keywords_with_categories(
        self,
        authed_client: AsyncClient,
        db_session: AsyncSession,
        sample_keyword: Keyword,
        sample_categories: list[Category],
    ) -> None:
        # Link keyword to a category
        cat = sample_categories[1]  # "quantum"
        link = KeywordCategoryLink(keyword_id=sample_keyword.id, category_id=cat.id)
        db_session.add(link)
        await db_session.commit()

        resp = await authed_client.get("/api/v1/keywords")
        data = resp.json()
        item = data["items"][0]
        assert len(item["categories"]) == 1
        assert item["categories"][0]["slug"] == "quantum"
        assert item["categories"][0]["name"] == "量子コンピュータ"

    async def test_camel_case_keys(
        self, authed_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await authed_client.get("/api/v1/keywords")
        item = resp.json()["items"][0]
        assert "articleCount" in item
        assert "createdAt" in item
        assert "categories" in item


@pytest.mark.asyncio
class TestCreateKeyword:
    async def test_create_success(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={"keyword": "Materials Informatics"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["keyword"] == "Materials Informatics"
        assert data["categories"] == []
        assert data["articleCount"] == 0

    async def test_create_with_categories(
        self,
        admin_client: AsyncClient,
        sample_categories: list[Category],
    ) -> None:
        cat = sample_categories[0]  # "ai_ml"
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={"keyword": "Edge AI", "categoryIds": [cat.id]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["categories"]) == 1
        assert data["categories"][0]["slug"] == "ai_ml"

    async def test_create_with_invalid_category_id(
        self, admin_client: AsyncClient
    ) -> None:
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={"keyword": "Bad Cat", "categoryIds": [99999]},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()

    async def test_create_duplicate_409(
        self, admin_client: AsyncClient, sample_keyword: Keyword
    ) -> None:
        resp = await admin_client.post(
            "/api/v1/keywords",
            json={"keyword": "Quantum Computing"},
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


@pytest.mark.asyncio
class TestUpdateKeyword:
    async def test_update_categories(
        self,
        admin_client: AsyncClient,
        sample_keyword: Keyword,
        sample_categories: list[Category],
    ) -> None:
        cat = sample_categories[2]  # "semiconductor"
        resp = await admin_client.patch(
            f"/api/v1/keywords/{sample_keyword.id}",
            json={"categoryIds": [cat.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["categories"]) == 1
        assert data["categories"][0]["slug"] == "semiconductor"

    async def test_update_clear_categories(
        self,
        admin_client: AsyncClient,
        db_session: AsyncSession,
        sample_keyword: Keyword,
        sample_categories: list[Category],
    ) -> None:
        # First add a category
        cat = sample_categories[0]
        link = KeywordCategoryLink(keyword_id=sample_keyword.id, category_id=cat.id)
        db_session.add(link)
        await db_session.commit()

        # Then clear categories
        resp = await admin_client.patch(
            f"/api/v1/keywords/{sample_keyword.id}",
            json={"categoryIds": []},
        )
        assert resp.status_code == 200
        assert resp.json()["categories"] == []

    async def test_update_not_found(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.patch(
            "/api/v1/keywords/99999",
            json={"categoryIds": []},
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
