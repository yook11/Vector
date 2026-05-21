"""セマンティック検索 (GET /api/v1/articles/search) のテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource, SourceType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_EMBEDDING_A = [0.1] * 768  # クエリに "近い"
FAKE_EMBEDDING_B = [0.9] * 768  # クエリから "遠い"
FAKE_QUERY_EMBEDDING = [0.1] * 768  # A にマッチ


async def _create_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="Test Source",
        source_type=SourceType.RSS,
        site_url="https://example.com",
        endpoint_url="https://example.com/feed.xml",
    )
    db_session.add(source)
    await db_session.flush()
    return source


async def _create_article(
    db_session: AsyncSession,
    source: NewsSource,
    *,
    category_id: int,
    title: str = "Test Article",
    url: str = "https://example.com/1",
    embedding: list[float] | None = None,
) -> Article:
    article = Article(
        source_id=source.id,
        source_url=url,
        original_title=title,
        original_content="Search test content.",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()

    extraction = ArticleCuration(
        article_id=article.id,
        translated_title=f"Translated: {title}",
        summary="Test summary",
    )
    db_session.add(extraction)
    await db_session.flush()

    # InScopeAssessment は INNER JOIN のため常に作成し、embedding があれば付与する
    analysis = InScopeAssessment(
        curation_id=extraction.id,
        translated_title=f"Translated: {title}",
        summary="Test summary",
        investor_take="Test investor_take",
        embedding=embedding,
        category_id=category_id,
    )
    db_session.add(analysis)
    await db_session.flush()

    return article


def _patch_embed_query(return_value: list[float] = FAKE_QUERY_EMBEDDING):
    """embed_search_query を固定ベクトルを返すように patch する。"""
    return patch(
        "app.search.service.embed_search_query",
        new_callable=AsyncMock,
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# A. Basic semantic search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["   ", "a" * 201])
async def test_semantic_search_rejects_invalid_q(
    authed_client: AsyncClient, q: str
) -> None:
    """auth 済の前提で q バリデーションを検証する。

    PR3 で endpoint が認証必須化されたため、anon + invalid q の cross-product は
    FastAPI 依存解決順 (422 vs 401) が挙動依存となり test 不安定化を招くので
    あえて書かない。auth 通過後の振る舞いに絞る。
    """
    resp = await authed_client.get("/api/v1/articles/search", params={"q": q})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_semantic_search_returns_articles_with_embedding(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """GET /api/v1/articles/search?q=test は embedding 付きの記事を返す。"""
    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        category_id=sample_categories[0].id,
        title="AI Breakthrough",
        url="https://example.com/ai",
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    with _patch_embed_query():
        resp = await authed_client.get(
            "/api/v1/articles/search", params={"q": "AI research"}
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any("AI Breakthrough" in item["translatedTitle"] for item in data["items"])


@pytest.mark.asyncio
async def test_semantic_search_excludes_articles_without_embedding(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """embedding の無い記事はセマンティック検索結果から除外される。"""
    source = await _create_source(db_session)
    cat_id = sample_categories[0].id
    await _create_article(
        db_session,
        source,
        category_id=cat_id,
        title="With Embedding",
        url="https://example.com/with",
        embedding=FAKE_EMBEDDING_A,
    )
    await _create_article(
        db_session,
        source,
        category_id=cat_id,
        title="Without Embedding",
        url="https://example.com/without",
        embedding=None,
    )
    await db_session.commit()

    with _patch_embed_query():
        resp = await authed_client.get("/api/v1/articles/search", params={"q": "test"})

    assert resp.status_code == 200
    data = resp.json()
    titles = [item["translatedTitle"] for item in data["items"]]
    assert "Translated: With Embedding" in titles
    assert "Translated: Without Embedding" not in titles


# ---------------------------------------------------------------------------
# B. No q parameter -- existing behavior unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_q_parameter_returns_analyzed_articles(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """q パラメータなしでは分析済み記事のみを返す。"""
    source = await _create_source(db_session)
    cat_id = sample_categories[0].id
    await _create_article(
        db_session,
        source,
        category_id=cat_id,
        title="Article 1",
        url="https://example.com/1",
        embedding=FAKE_EMBEDDING_A,
    )
    await _create_article(
        db_session,
        source,
        category_id=cat_id,
        title="Article 2",
        url="https://example.com/2",
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    # embed_search_query は呼ばれないので patch 不要
    resp = await authed_client.get("/api/v1/articles")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2


# ---------------------------------------------------------------------------
# D. Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_returns_503_on_embedding_failure(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """embedding 生成が provider/infra 起因で失敗した場合は 503 を返す。"""
    from app.search.errors import SearchError

    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        category_id=sample_categories[0].id,
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    with patch(
        "app.search.service.embed_search_query",
        new_callable=AsyncMock,
        side_effect=SearchError("API down"),
    ):
        resp = await authed_client.get("/api/v1/articles/search", params={"q": "test"})

    assert resp.status_code == 503
    assert "embedding" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_semantic_search_returns_422_on_unexpected_embedder_error(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """embedder が ``AIProviderError`` 以外の例外を漏らした場合は 422 を返す。

    Schemathesis ``not_a_server_error`` は 5xx 全部を fail にするため、
    翻訳の網を抜けた例外をユーザー入力起因として 4xx に分類する経路を担保する。
    """
    from app.exceptions import InvalidQueryError

    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        category_id=sample_categories[0].id,
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    with patch(
        "app.search.service.embed_search_query",
        new_callable=AsyncMock,
        side_effect=InvalidQueryError(
            "Could not generate embedding for the search query."
        ),
    ):
        resp = await authed_client.get("/api/v1/articles/search", params={"q": "test"})

    assert resp.status_code == 422
    assert "embedding" in resp.json()["detail"].lower()


async def _invoke_embed_search_query(fake_embedder: MagicMock) -> None:
    """``embed_search_query`` を fake redis + cache miss で呼ぶ shared helper。"""
    from uuid import uuid4

    from app.search.service import embed_search_query

    fake_redis = MagicMock()
    fake_redis.eval = AsyncMock(return_value=1)

    with (
        patch(
            "app.search.embedding_cache.get_query_embedding",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.search.embedding_cache.set_query_embedding",
            new_callable=AsyncMock,
        ),
    ):
        await embed_search_query(
            "test query",
            user_id=uuid4(),
            redis=fake_redis,
            daily_max=10,
            embedder=fake_embedder,
        )


@pytest.mark.asyncio
async def test_embed_search_query_translates_unexpected_to_invalid_query() -> None:
    """翻訳網を抜けた非 ``AIProviderError`` (e.g. RuntimeError) は
    ``InvalidQueryError`` (422) へ振る (Schemathesis 5xx fail を避ける + translator
    バグの追跡)。"""
    from app.exceptions import InvalidQueryError

    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(side_effect=RuntimeError("translator gap"))

    with pytest.raises(InvalidQueryError):
        await _invoke_embed_search_query(fake_embedder)


@pytest.mark.asyncio
async def test_embed_search_query_routes_service_unavailable_to_search_error() -> None:
    """``AIProviderServiceUnavailableError`` (provider 5xx) → ``SearchError`` (503)。"""
    from app.analysis.ai_provider_errors import AIProviderServiceUnavailableError
    from app.search.errors import SearchError

    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(
        side_effect=AIProviderServiceUnavailableError("upstream 5xx")
    )

    with pytest.raises(SearchError):
        await _invoke_embed_search_query(fake_embedder)


@pytest.mark.asyncio
async def test_embed_search_query_routes_configuration_to_search_error() -> None:
    """``AIProviderConfigurationError`` (auth / key 不正) は ``SearchError`` (503)。"""
    from app.analysis.ai_provider_errors import AIProviderConfigurationError
    from app.search.errors import SearchError

    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(
        side_effect=AIProviderConfigurationError("API key missing")
    )

    with pytest.raises(SearchError):
        await _invoke_embed_search_query(fake_embedder)


@pytest.mark.asyncio
async def test_embed_search_query_routes_request_invalid_to_search_error() -> None:
    """``AIProviderRequestInvalidError`` (provider response shape 違反) は 503。

    user query の内容では直らない provider 側の障害なので infra 系に振る。
    """
    from app.analysis.ai_provider_errors import AIProviderRequestInvalidError
    from app.search.errors import SearchError

    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(
        side_effect=AIProviderRequestInvalidError("no embeddings")
    )

    with pytest.raises(SearchError):
        await _invoke_embed_search_query(fake_embedder)


@pytest.mark.asyncio
async def test_embed_search_query_routes_rate_limited_to_search_error() -> None:
    """``AIProviderRateLimitedError`` (429) は ``SearchError`` (503)。"""
    from app.analysis.ai_provider_errors import AIProviderRateLimitedError
    from app.search.errors import SearchError

    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(
        side_effect=AIProviderRateLimitedError("rate limited")
    )

    with pytest.raises(SearchError):
        await _invoke_embed_search_query(fake_embedder)


@pytest.mark.asyncio
async def test_embed_search_query_routes_input_rejected_to_invalid_query() -> None:
    """``AIProviderInputRejectedError`` (safety filter blocked) は user query → 422。

    503 の infra 系には振らず、user に「クエリの内容を変えて再試行を」と促す。
    """
    from app.analysis.ai_provider_errors import AIProviderInputRejectedError
    from app.exceptions import InvalidQueryError

    fake_embedder = MagicMock()
    fake_embedder.embed_query = AsyncMock(
        side_effect=AIProviderInputRejectedError("safety blocked")
    )

    with pytest.raises(InvalidQueryError):
        await _invoke_embed_search_query(fake_embedder)


# ---------------------------------------------------------------------------
# E. red-team C1 対策: auth + per-user quota の統合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_returns_401_when_unauthenticated(
    client: AsyncClient,
) -> None:
    """anon access は 401 を返す (red-team C1 対策: anon DoS 入口の閉鎖)。"""
    resp = await client.get("/api/v1/articles/search", params={"q": "test"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_semantic_search_returns_429_when_quota_exhausted(
    authed_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """quota 枯渇時は 429 を返す (red-team C1 対策: per-user 課金キャップ)。

    cache miss を強制し、fake redis の eval を 0 (枯渇) で固定して assert する。
    embedder 呼出直前で fail-fast するので Gemini API には届かない。
    """
    from app.dependencies import get_redis_client
    from app.main import app as fastapi_app

    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        category_id=sample_categories[0].id,
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    fake_redis = MagicMock()
    fake_redis.eval = AsyncMock(return_value=0)
    fastapi_app.dependency_overrides[get_redis_client] = lambda: fake_redis
    try:
        with (
            patch(
                "app.search.embedding_cache.get_query_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.search.embedding_cache.set_query_embedding",
                new_callable=AsyncMock,
            ),
        ):
            resp = await authed_client.get(
                "/api/v1/articles/search", params={"q": "test"}
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_redis_client, None)

    assert resp.status_code == 429
    assert "quota" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_user_consumes_same_quota_as_regular_user(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    sample_categories: list[Category],
) -> None:
    """admin も通常 user と同じ quota を消費する (構造的抜け道の不在を担保)。

    admin が課金抜け道になるリスクを排除する設計判断 (memory feedback_no_share_
    different_problems): admin / user で quota を分けない。
    """
    from app.dependencies import get_redis_client
    from app.main import app as fastapi_app
    from app.search.router import get_embedder_for_search

    source = await _create_source(db_session)
    await _create_article(
        db_session,
        source,
        category_id=sample_categories[0].id,
        embedding=FAKE_EMBEDDING_A,
    )
    await db_session.commit()

    fake_redis = MagicMock()
    fake_redis.eval = AsyncMock(return_value=1)
    fake_embedder = MagicMock(embed_query=AsyncMock(return_value=FAKE_QUERY_EMBEDDING))
    fastapi_app.dependency_overrides[get_redis_client] = lambda: fake_redis
    fastapi_app.dependency_overrides[get_embedder_for_search] = lambda: fake_embedder
    try:
        with (
            patch(
                "app.search.embedding_cache.get_query_embedding",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.search.embedding_cache.set_query_embedding",
                new_callable=AsyncMock,
            ),
        ):
            resp = await admin_client.get(
                "/api/v1/articles/search", params={"q": "test"}
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_redis_client, None)
        fastapi_app.dependency_overrides.pop(get_embedder_for_search, None)

    assert resp.status_code == 200
    fake_redis.eval.assert_called_once()
