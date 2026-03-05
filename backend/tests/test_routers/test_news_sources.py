"""Tests for the news_sources CRUD API."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.news_source import NewsSource


async def test_list_sources_empty(
    authed_client: AsyncClient,
) -> None:
    response = await authed_client.get("/api/v1/sources")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


async def test_list_sources(
    authed_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    source = NewsSource(
        name="TechCrunch",
        source_type="rss",
        feed_url="https://techcrunch.com/feed/",
    )
    db_session.add(source)
    await db_session.commit()

    response = await authed_client.get("/api/v1/sources")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "TechCrunch"
    assert data["items"][0]["sourceType"] == "rss"


async def test_get_source(
    authed_client: AsyncClient,
    sample_source: NewsSource,
) -> None:
    response = await authed_client.get(f"/api/v1/sources/{sample_source.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == sample_source.name
    assert data["feedUrl"] == sample_source.feed_url


async def test_get_source_not_found(
    authed_client: AsyncClient,
) -> None:
    response = await authed_client.get("/api/v1/sources/999")
    assert response.status_code == 404


async def test_create_rss_source(
    authed_client: AsyncClient,
) -> None:
    body = {
        "name": "New RSS Source",
        "sourceType": "rss",
        "feedUrl": "https://example.com/rss.xml",
        "fetchIntervalMinutes": 360,
    }
    response = await authed_client.post("/api/v1/sources", json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "New RSS Source"
    assert data["sourceType"] == "rss"
    assert data["feedUrl"] == "https://example.com/rss.xml"
    assert data["fetchIntervalMinutes"] == 360
    assert data["isActive"] is True
    assert data["consecutiveErrors"] == 0


async def test_create_api_source(
    authed_client: AsyncClient,
) -> None:
    body = {
        "name": "Hacker News",
        "sourceType": "api",
        "apiEndpoint": "hacker-news",
    }
    response = await authed_client.post("/api/v1/sources", json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["sourceType"] == "api"
    assert data["apiEndpoint"] == "hacker-news"
    assert data["feedUrl"] is None


async def test_create_rss_source_missing_feed_url(
    authed_client: AsyncClient,
) -> None:
    body = {
        "name": "No Feed URL",
        "sourceType": "rss",
    }
    response = await authed_client.post("/api/v1/sources", json=body)
    assert response.status_code == 400
    assert "feed_url" in response.json()["detail"]


async def test_update_source(
    authed_client: AsyncClient,
    sample_source: NewsSource,
) -> None:
    body = {
        "name": "Updated Name",
        "fetchIntervalMinutes": 60,
    }
    response = await authed_client.put(
        f"/api/v1/sources/{sample_source.id}", json=body
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["fetchIntervalMinutes"] == 60


async def test_update_source_not_found(
    authed_client: AsyncClient,
) -> None:
    response = await authed_client.put("/api/v1/sources/999", json={"name": "x"})
    assert response.status_code == 404


async def test_delete_source(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    response = await authed_client.delete(f"/api/v1/sources/{sample_source.id}")
    assert response.status_code == 204

    # Verify deleted
    stmt = select(NewsSource).where(NewsSource.id == sample_source.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is None


async def test_delete_source_not_found(
    authed_client: AsyncClient,
) -> None:
    response = await authed_client.delete("/api/v1/sources/999")
    assert response.status_code == 404


async def test_toggle_source_active(
    authed_client: AsyncClient,
    sample_source: NewsSource,
) -> None:
    # Initially active
    assert sample_source.is_active is True

    # Toggle off
    response = await authed_client.patch(
        f"/api/v1/sources/{sample_source.id}/toggle"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["isActive"] is False

    # Toggle back on
    response = await authed_client.patch(
        f"/api/v1/sources/{sample_source.id}/toggle"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["isActive"] is True
    # next_fetch_at should be None for immediate fetch
    assert data["nextFetchAt"] is None


async def test_toggle_source_not_found(
    authed_client: AsyncClient,
) -> None:
    response = await authed_client.patch("/api/v1/sources/999/toggle")
    assert response.status_code == 404


async def test_unauthenticated_access(
    client: AsyncClient,
) -> None:
    """All source endpoints require authentication."""
    response = await client.get("/api/v1/sources")
    assert response.status_code == 401
