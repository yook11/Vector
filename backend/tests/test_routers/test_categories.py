"""Tests for /api/v1/categories router endpoints."""

import pytest
from httpx import AsyncClient

from app.models.investment_category import InvestmentCategory


@pytest.mark.asyncio
class TestListCategories:
    async def test_empty_list(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    async def test_returns_all_categories(
        self,
        client: AsyncClient,
        sample_categories: list[InvestmentCategory],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 6

        slugs = [item["slug"] for item in data["items"]]
        assert "growth_catalyst" in slugs
        assert "financial_signal" in slugs

    async def test_default_locale_ja(
        self,
        client: AsyncClient,
        sample_categories: list[InvestmentCategory],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        name_map = {item["slug"]: item["name"] for item in items}
        assert name_map["growth_catalyst"] == "成長期待"
        assert name_map["financial_signal"] == "業績シグナル"

    async def test_locale_en(
        self,
        client: AsyncClient,
        sample_categories: list[InvestmentCategory],
    ) -> None:
        resp = await client.get("/api/v1/categories?locale=en")
        items = resp.json()["items"]
        name_map = {item["slug"]: item["name"] for item in items}
        assert name_map["growth_catalyst"] == "Growth Catalyst"
        assert name_map["financial_signal"] == "Financial Signal"

    async def test_camel_case_response(
        self,
        client: AsyncClient,
        sample_categories: list[InvestmentCategory],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        item = resp.json()["items"][0]
        assert "name" in item
        assert "slug" in item
        assert "description" in item

    async def test_ordered_by_slug(
        self,
        client: AsyncClient,
        sample_categories: list[InvestmentCategory],
    ) -> None:
        resp = await client.get("/api/v1/categories")
        items = resp.json()["items"]
        slugs = [item["slug"] for item in items]
        assert slugs == sorted(slugs)

    async def test_no_auth_required(self, client: AsyncClient) -> None:
        """Categories endpoint should not require authentication."""
        resp = await client.get("/api/v1/categories")
        assert resp.status_code == 200
