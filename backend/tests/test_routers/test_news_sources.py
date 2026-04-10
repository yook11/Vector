"""Tests for the news_sources CRUD API."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.news_source import NewsSource, SourceType


async def test_list_sources_empty(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/api/v1/admin/sources")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []


async def test_list_sources(
    admin_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    source = NewsSource(
        name="TechCrunch",
        source_type=SourceType.RSS,
        site_url="https://techcrunch.com",
        endpoint_url="https://techcrunch.com/feed/",
    )
    db_session.add(source)
    await db_session.commit()

    response = await admin_client.get("/api/v1/admin/sources")
    assert response.status_code == 200
    data = response.json()
    assert data["items"][0]["name"] == "TechCrunch"
    assert data["items"][0]["sourceType"] == "rss"
    assert data["items"][0]["endpointUrl"] == "https://techcrunch.com/feed/"


async def test_list_sources_forbidden_for_non_admin(
    authed_client: AsyncClient,
) -> None:
    """Non-admin users get 403 on the management list endpoint."""
    response = await authed_client.get("/api/v1/admin/sources")
    assert response.status_code == 403



async def test_create_rss_source(
    admin_client: AsyncClient,
) -> None:
    body = {
        "name": "New RSS Source",
        "sourceType": "rss",
        "siteUrl": "https://example.com",
        "endpointUrl": "https://example.com/rss.xml",
    }
    response = await admin_client.post("/api/v1/admin/sources", json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "New RSS Source"
    assert data["sourceType"] == "rss"
    assert data["endpointUrl"] == "https://example.com/rss.xml"
    assert data["siteUrl"] == "https://example.com"
    assert data["isActive"] is True


async def test_create_api_source(
    admin_client: AsyncClient,
) -> None:
    body = {
        "name": "Hacker News",
        "sourceType": "api",
        "siteUrl": "https://news.ycombinator.com",
        "endpointUrl": "https://hn.algolia.com/api/v1/search_by_date",
    }
    response = await admin_client.post("/api/v1/admin/sources", json=body)
    assert response.status_code == 201
    data = response.json()
    assert data["sourceType"] == "api"
    assert data["endpointUrl"] == "https://hn.algolia.com/api/v1/search_by_date"


async def test_create_source_missing_endpoint_url(
    admin_client: AsyncClient,
) -> None:
    body = {
        "name": "No Endpoint",
        "sourceType": "rss",
        "siteUrl": "https://example.com",
    }
    response = await admin_client.post("/api/v1/admin/sources", json=body)
    assert response.status_code == 422


async def test_create_source_missing_site_url(
    admin_client: AsyncClient,
) -> None:
    body = {
        "name": "No Site URL",
        "sourceType": "rss",
        "endpointUrl": "https://example.com/feed.xml",
    }
    response = await admin_client.post("/api/v1/admin/sources", json=body)
    assert response.status_code == 422


async def test_delete_source(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    sample_source: NewsSource,
) -> None:
    response = await admin_client.delete(f"/api/v1/admin/sources/{sample_source.id}")
    assert response.status_code == 204

    # Verify deleted
    stmt = select(NewsSource).where(NewsSource.id == sample_source.id)
    result = await db_session.execute(stmt)
    assert result.scalar_one_or_none() is None


async def test_delete_source_not_found(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.delete("/api/v1/admin/sources/999")
    assert response.status_code == 404


async def test_toggle_source_active(
    admin_client: AsyncClient,
    sample_source: NewsSource,
) -> None:
    # Initially active
    assert sample_source.is_active is True

    # Toggle off
    response = await admin_client.patch(f"/api/v1/admin/sources/{sample_source.id}/toggle")
    assert response.status_code == 200
    data = response.json()
    assert data["isActive"] is False

    # Toggle back on
    response = await admin_client.patch(f"/api/v1/admin/sources/{sample_source.id}/toggle")
    assert response.status_code == 200
    data = response.json()
    assert data["isActive"] is True


async def test_toggle_source_not_found(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.patch("/api/v1/admin/sources/999/toggle")
    assert response.status_code == 404


async def test_missing_auth_headers(
    client: AsyncClient,
) -> None:
    """Missing required headers return 422 (FastAPI type validation)."""
    response = await client.get("/api/v1/admin/sources")
    assert response.status_code == 422
