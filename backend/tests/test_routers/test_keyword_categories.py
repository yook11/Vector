"""Tests for /api/v1/keyword-categories router endpoints."""

import pytest
from httpx import AsyncClient

from app.models.keyword_category import KeywordCategory


@pytest.mark.asyncio
class TestListKeywordCategories:
    async def test_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/keyword-categories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    async def test_returns_all_categories(
        self,
        client: AsyncClient,
        sample_keyword_categories: list[KeywordCategory],
    ) -> None:
        resp = await client.get("/api/v1/keyword-categories")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3

        slugs = [item["slug"] for item in data["items"]]
        assert "ai_ml" in slugs
        assert "quantum" in slugs
        assert "semiconductor" in slugs

    async def test_default_locale_ja(
        self,
        client: AsyncClient,
        sample_keyword_categories: list[KeywordCategory],
    ) -> None:
        resp = await client.get("/api/v1/keyword-categories")
        items = resp.json()["items"]
        name_map = {item["slug"]: item["name"] for item in items}
        assert name_map["ai_ml"] == "AI・ML"
        assert name_map["quantum"] == "量子コンピュータ"

    async def test_locale_en(
        self,
        client: AsyncClient,
        sample_keyword_categories: list[KeywordCategory],
    ) -> None:
        resp = await client.get("/api/v1/keyword-categories?locale=en")
        items = resp.json()["items"]
        name_map = {item["slug"]: item["name"] for item in items}
        assert name_map["ai_ml"] == "AI & ML"
        assert name_map["quantum"] == "Quantum Computing"

    async def test_ordered_by_slug(
        self,
        client: AsyncClient,
        sample_keyword_categories: list[KeywordCategory],
    ) -> None:
        resp = await client.get("/api/v1/keyword-categories")
        items = resp.json()["items"]
        slugs = [item["slug"] for item in items]
        assert slugs == sorted(slugs)

    async def test_no_auth_required(self, client: AsyncClient) -> None:
        """Keyword categories endpoint should not require authentication."""
        resp = await client.get("/api/v1/keyword-categories")
        assert resp.status_code == 200

    async def test_response_has_id(
        self,
        client: AsyncClient,
        sample_keyword_categories: list[KeywordCategory],
    ) -> None:
        resp = await client.get("/api/v1/keyword-categories")
        item = resp.json()["items"][0]
        assert "id" in item
        assert "slug" in item
        assert "name" in item
