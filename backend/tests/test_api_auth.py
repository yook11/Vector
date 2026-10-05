"""実際の業務 API の認証境界と、リクエスト単位の依存関係共有を検証する。"""

import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock, Mock

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from httpx2 import ASGITransport, AsyncClient

from app import dependencies, main
from app.admin.sources.router import get_news_source_service
from app.dependencies import (
    AuthenticatedUser,
    require_admin_user,
    require_authenticated_user,
    require_bff_request,
)
from app.routers.articles import get_article_service
from app.routers.watchlist import get_watchlist_service
from app.schemas.articles import AnalyzedArticlePreviewList
from tests.conftest import TEST_ADMIN_ID, TEST_USER_ID, make_internal_jwt

pytestmark = pytest.mark.unit


def _api_routes(app: FastAPI) -> list[RouteContext]:
    return [
        route
        for route in iter_route_contexts(app.routes)
        if isinstance(route.original_route, APIRoute)
    ]


@pytest.fixture
def api_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = main.app
    monkeypatch.setattr(app, "dependency_overrides", {})
    return app


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        yield client


def test_all_business_routes_have_common_bff_guard(api_app: FastAPI) -> None:
    """health 以外の全 API は、スキーマへの掲載有無によらず BFF ガードを持つ。"""
    routes = _api_routes(api_app)
    assert routes
    missing = [
        (route.path, route.methods)
        for route in routes
        if (route.path, route.methods) != ("/api/v1/health", {"GET"})
        and require_bff_request
        not in [dependency.call for dependency in route.dependant.dependencies]
    ]
    assert missing == []


@pytest.mark.parametrize(
    ("prefix", "guard"),
    [
        ("/api/v1/me/", require_authenticated_user),
        ("/api/v1/research/", require_authenticated_user),
        ("/api/v1/admin/", require_admin_user),
    ],
)
def test_feature_routes_require_their_access_guard(
    api_app: FastAPI, prefix: str, guard: object
) -> None:
    """各機能の全 API は、引数でユーザーを受け取らなくても認証を要求する。"""
    routes = [route for route in _api_routes(api_app) if route.path.startswith(prefix)]
    assert routes
    assert [
        route.path
        for route in routes
        if guard not in [dependency.call for dependency in route.dependant.dependencies]
    ] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        (method, route.path)
        for route in _api_routes(main.app)
        for method in sorted(route.methods)
        if (route.path, method) != ("/api/v1/health", "GET")
    ],
)
async def test_business_endpoint_rejects_missing_bff_jwt(
    api_client: AsyncClient, method: str, path: str
) -> None:
    """全業務 API が認証ヘッダーのない要求を 401 で拒否する。"""
    url = re.sub(r"\{[^}]+\}", "1", path)
    response = await api_client.request(method, url)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


async def test_shared_articles_accept_bff_only_token(
    api_app: FastAPI, api_client: AsyncClient, bff_headers: dict[str, str]
) -> None:
    """記事の共有 read は、ユーザー情報のない BFF JWT で利用できる。"""
    service = SimpleNamespace(
        list_articles=AsyncMock(
            return_value=AnalyzedArticlePreviewList(items=[], next_cursor=None)
        )
    )
    api_app.dependency_overrides[get_article_service] = lambda: service
    response = await api_client.get("/api/v1/articles", headers=bff_headers)
    assert response.status_code == 200
    assert response.json() == {"items": [], "nextCursor": None}


@pytest.mark.parametrize("path", ["/api/v1/me/watchlist", "/api/v1/research/threads"])
async def test_user_endpoint_rejects_bff_only_token(
    api_client: AsyncClient, path: str, bff_headers: dict[str, str]
) -> None:
    """ログイン必須 API は、署名が有効でもユーザー情報がなければ拒否する。"""
    response = await api_client.get(path, headers=bff_headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize("path", ["/api/v1/me/watchlist", "/api/v1/research/threads"])
async def test_user_endpoint_rejects_invalid_user_id(
    api_client: AsyncClient, path: str
) -> None:
    """ユーザー ID が UUID でなければ、署名が有効でもログインを認めない。"""
    token = make_internal_jwt("invalid-user-id")
    response = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/me/watchlist", "/api/v1/research/threads"])
async def test_user_endpoint_rejects_unknown_role(
    api_client: AsyncClient, path: str
) -> None:
    """未定義の role は、署名が有効でもログインを認めない。"""
    token = make_internal_jwt(TEST_USER_ID, role="unknown")
    response = await api_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_admin_endpoint_rejects_bff_only_token(
    api_client: AsyncClient, bff_headers: dict[str, str]
) -> None:
    """管理 API もユーザー情報を持たない BFF JWT を 401 で拒否する。"""
    response = await api_client.get("/api/v1/admin/sources", headers=bff_headers)
    assert response.status_code == 401


async def test_admin_endpoint_rejects_regular_user(
    api_client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """認証済みの一般ユーザーによる管理 API 利用は 403 で拒否する。"""
    response = await api_client.get("/api/v1/admin/sources", headers=auth_headers)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin access required"}


@pytest.fixture
def watchlist_user_ids(api_app: FastAPI) -> list[str]:
    user_ids: list[str] = []

    def service(
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ):
        user_ids.append(str(user.id))
        return SimpleNamespace(list_watched_ids=AsyncMock(return_value=[]))

    api_app.dependency_overrides[get_watchlist_service] = service
    return user_ids


async def test_watchlist_reuses_authentication_within_request(
    api_client: AsyncClient,
    auth_headers: dict[str, str],
    watchlist_user_ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共通ガード・機能ガード・サービス引数は、同じ認証結果を一度だけ構築する。"""
    decode = Mock(wraps=jwt.decode)
    user_from_claims = Mock(wraps=dependencies._user_from_claims)
    monkeypatch.setattr(jwt, "decode", decode)
    monkeypatch.setattr(dependencies, "_user_from_claims", user_from_claims)

    response = await api_client.get(
        "/api/v1/me/watchlist/ids", params={"articleIds": 1}, headers=auth_headers
    )

    assert response.status_code == 200
    decode.assert_called_once()
    user_from_claims.assert_called_once()
    assert watchlist_user_ids == [TEST_USER_ID]


async def test_admin_reuses_bff_verification_within_request(
    api_app: FastAPI,
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理者認証まで辿っても、JWT とユーザーの検証を重複させない。"""
    service = SimpleNamespace(get_all=AsyncMock(return_value={"items": []}))
    api_app.dependency_overrides[get_news_source_service] = lambda: service
    decode = Mock(wraps=jwt.decode)
    user_from_claims = Mock(wraps=dependencies._user_from_claims)
    monkeypatch.setattr(jwt, "decode", decode)
    monkeypatch.setattr(dependencies, "_user_from_claims", user_from_claims)
    token = make_internal_jwt(TEST_ADMIN_ID, role="admin")

    response = await api_client.get(
        "/api/v1/admin/sources", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    decode.assert_called_once()
    user_from_claims.assert_called_once()


async def test_authentication_is_not_shared_between_requests(
    api_client: AsyncClient,
    watchlist_user_ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別のリクエストでは JWT を検証し直し、そのリクエストのユーザーを渡す。"""
    decode = Mock(wraps=jwt.decode)
    user_from_claims = Mock(wraps=dependencies._user_from_claims)
    monkeypatch.setattr(jwt, "decode", decode)
    monkeypatch.setattr(dependencies, "_user_from_claims", user_from_claims)
    for user_id in [TEST_USER_ID, TEST_ADMIN_ID]:
        token = make_internal_jwt(user_id)
        response = await api_client.get(
            "/api/v1/me/watchlist/ids",
            params={"articleIds": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
    assert decode.call_count == 2
    assert user_from_claims.call_count == 2
    assert watchlist_user_ids == [TEST_USER_ID, TEST_ADMIN_ID]


async def test_health_does_not_require_or_decode_jwt(
    api_app: FastAPI, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ヘルスチェックは JWT を検証せず、認証なしで利用できる。"""
    connection = AsyncMock()
    engine = Mock()
    engine.connect.return_value = AsyncMock()
    engine.connect.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(api_app.state, "engine", engine, raising=False)
    decode = Mock(wraps=jwt.decode)
    monkeypatch.setattr(jwt, "decode", decode)

    response = await api_client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "dbConnected": True}
    decode.assert_not_called()
