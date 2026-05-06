"""セマンティック検索 (GET /api/v1/articles/search) のテスト。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.article_analysis import ArticleAnalysis
from app.models.article_extraction import ArticleExtraction
from app.models.category import Category
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
    topic: str = "search test",
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

    extraction = ArticleExtraction(
        article_id=article.id,
        translated_title=f"Translated: {title}",
        summary="Test summary",
        ai_model="gemini-2.0-flash",
    )
    db_session.add(extraction)
    await db_session.flush()

    # ArticleAnalysis は INNER JOIN のため常に作成し、embedding があれば付与する
    analysis = ArticleAnalysis(
        extraction_id=extraction.id,
        translated_title=f"Translated: {title}",
        summary="Test summary",
        investor_take="Test investor_take",
        ai_model="gemini-2.0-flash",
        embedding=embedding,
        embedding_model="text-embedding-004" if embedding else None,
        topic=topic,
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
    """embedding 生成が失敗した場合は 503 を返す。"""
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
            patch(
                "app.search.service.get_embedder",
                return_value=MagicMock(
                    embed_query=AsyncMock(return_value=FAKE_QUERY_EMBEDDING)
                ),
            ),
        ):
            resp = await admin_client.get(
                "/api/v1/articles/search", params={"q": "test"}
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_redis_client, None)

    assert resp.status_code == 200
    fake_redis.eval.assert_called_once()
