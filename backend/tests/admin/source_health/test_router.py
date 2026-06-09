"""GET /api/v1/admin/sources/health のルーターテスト (認可 / 検証 / レスポンス形)。"""

import pytest
from httpx import AsyncClient

from app.models.news_source import NewsSource

_HEALTH_URL = "/api/v1/admin/sources/health"

_TOP_KEYS = {"windowHours", "observedAt", "items"}
_ITEM_KEYS = {
    "sourceId",
    "sourceName",
    "sourceType",
    "isActive",
    "analyzableRate",
    "analyzableCount",
    "processedArticleCount",
    "incompleteCount",
    "failureReasons",
    "lastSucceededAt",
}
# data 最小化: source 詳細 / free-text error / payload を露出しない。
_FORBIDDEN_ITEM_KEYS = {"siteUrl", "endpointUrl", "errorMessage", "payload"}


async def test_source_health_requires_auth(client: AsyncClient) -> None:
    """未認証アクセスは 401。"""
    response = await client.get(_HEALTH_URL)
    assert response.status_code == 401


async def test_source_health_forbidden_for_non_admin(
    authed_client: AsyncClient,
) -> None:
    """一般ユーザーは admin 依存で 403。"""
    response = await authed_client.get(_HEALTH_URL)
    assert response.status_code == 403


async def test_source_health_ok_for_admin_empty_db(
    admin_client: AsyncClient,
) -> None:
    """admin は 200。source 未登録なら items は空、windowHours は既定 24。"""
    response = await admin_client.get(_HEALTH_URL)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _TOP_KEYS
    assert body["items"] == []
    assert body["windowHours"] == 24


@pytest.mark.parametrize("window_hours", [24, 48, 72, 168])
async def test_source_health_accepts_allowed_window_hours(
    admin_client: AsyncClient, window_hours: int
) -> None:
    """許可値 (query 指定の 24/48/72/168) はすべて 200 でエコーされる。"""
    response = await admin_client.get(_HEALTH_URL, params={"windowHours": window_hours})
    assert response.status_code == 200
    assert response.json()["windowHours"] == window_hours


@pytest.mark.parametrize("window_hours", [25, 0, 12, 169, 240])
async def test_source_health_rejects_invalid_window_hours(
    admin_client: AsyncClient, window_hours: int
) -> None:
    """許可外の windowHours は 422。"""
    response = await admin_client.get(_HEALTH_URL, params={"windowHours": window_hours})
    assert response.status_code == 422


async def test_source_health_item_uses_camel_case_keys(
    admin_client: AsyncClient, sample_source: NewsSource
) -> None:
    """item が camelCase キーちょうどを持つ。"""
    response = await admin_client.get(_HEALTH_URL)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == _ITEM_KEYS


async def test_source_health_omits_sensitive_fields(
    admin_client: AsyncClient, sample_source: NewsSource
) -> None:
    """item に URL / free-text error / payload を含めない。"""
    response = await admin_client.get(_HEALTH_URL)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert _FORBIDDEN_ITEM_KEYS.isdisjoint(body["items"][0])
