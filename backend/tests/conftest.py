"""バックエンドテスト共通のフィクスチャ。"""

import time
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.config import settings
from app.dependencies import get_session
from app.main import app
from app.models import (  # noqa: F401
    Article,
    ArticleAnalysis,
    ArticleExtraction,
    ArticleExtractionEntity,
    ArticleRejection,
    Category,
    DiscoveredArticle,
    FetchLog,
    NewsSource,
    SourceType,
    WatchlistEntry,
    WeeklyBriefing,
)

TEST_DATABASE_URL = settings.database_url.rsplit("/", 1)[0] + "/vector_test"
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

# --- BFF JWT auth helpers ---
# BFF (Next.js) は Better Auth セッションから user_id/role を取り出して
# HS256 JWT に署名し、backend に Authorization: Bearer で渡す。本ヘルパは
# テストで同じ secret を使って疑似 BFF として JWT を発行する。

TEST_USER_ID = "00000000-0000-4000-a000-000000000001"
TEST_ADMIN_ID = "00000000-0000-4000-a000-000000000002"
INTERNAL_SECRET = settings.internal_api_secret.get_secret_value()
_JWT_ALGORITHM = "HS256"
_JWT_TTL_SECONDS = 60


def make_internal_jwt(user_id: str, role: str = "user") -> str:
    """テスト用に BFF 模擬の HS256 JWT を発行する。"""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "iat": now,
            "exp": now + _JWT_TTL_SECONDS,
        },
        INTERNAL_SECRET,
        algorithm=_JWT_ALGORITHM,
    )


def _auth_headers(user_id: str, role: str = "user") -> dict[str, str]:
    """テスト用 Authorization: Bearer <jwt> ヘッダを組み立てる。"""
    return {"Authorization": f"Bearer {make_internal_jwt(user_id, role)}"}


_INTEGRATION_FIXTURES = frozenset(
    {
        "db_session",
        "client",
        "authed_client",
        "admin_client",
        "session_factory",
        "sample_categories",
        "sample_source",
        "sample_hn_source",
        "sample_av_source",
    }
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """fixture 依存から unit / integration マーカーを自動付与する。

    DB セッション・httpx Client・seed データなどの integration 用 fixture を
    要求するテストは integration、それ以外は unit として扱う。autouse の
    ensure_test_database / setup_db はパッケージ全体の前提なので無視する。
    """
    for item in items:
        fixtures = set(getattr(item, "fixturenames", ()))
        if fixtures & _INTEGRATION_FIXTURES:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)


_test_db_initialized = False


async def _ensure_test_database_once() -> None:
    """vector_test DB と pgvector / auth スキーマを確保する (idempotent, 初回のみ)。

    integration テスト初回起動時にだけ呼ばれる。unit テストのみの実行 (CI の
    backend-unit job 等、postgres service 無しの環境) ではそもそも呼ばれないため、
    DB 接続も発生しない。
    """
    global _test_db_initialized
    if _test_db_initialized:
        return
    base_url = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    engine = create_async_engine(
        base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'vector_test'")
        )
        if not result.scalar():
            await conn.execute(text("CREATE DATABASE vector_test"))
    await engine.dispose()

    async with engine_test.connect() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
        await conn.commit()
    _test_db_initialized = True


@pytest.fixture(autouse=True)
async def setup_db(request: pytest.FixtureRequest) -> AsyncGenerator[None]:
    """integration テストのみ、各テスト前にテーブルを作成し終了後に破棄する。

    unit テスト (pytest_collection_modifyitems で自動分類) は DB を触らないため
    create_all/drop_all を毎回流すのは純粋な無駄。integration マーカーが付いた
    テストにのみ DDL を流す。
    """
    if "integration" not in request.keywords:
        yield
        return
    await _ensure_test_database_once()
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # watchlist_entries.user_id の FK を満たすため auth.user を seed する
        await conn.execute(
            text(
                'INSERT INTO auth."user" (id) VALUES (:uid1), (:uid2) '
                "ON CONFLICT DO NOTHING"
            ),
            {"uid1": TEST_USER_ID, "uid2": TEST_ADMIN_ID},
        )
    yield
    async with engine_test.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)


@pytest.fixture
def session_factory() -> async_sessionmaker[AsyncSession]:
    """Service クラスのテスト用に session factory を提供する。"""
    return async_sessionmaker(
        engine_test,
        class_=SQLModelAsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """テスト用 DB セッションを提供する。"""
    async with SQLModelAsyncSession(engine_test, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """DI でセッションを差し替えた httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession]:
        # db_session には autobegin されたトランザクションが残ることがある
        # (seed の refresh など)。本番の get_session と同様に新しい
        # トランザクションを開始するため、ここで一度 commit しておく。
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """通常ユーザー用の BFF プロキシ認証ヘッダーを返す。"""
    return _auth_headers(TEST_USER_ID)


@pytest.fixture
async def authed_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient]:
    """BFF プロキシ認証ヘッダーを付与済みの httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession]:
        # db_session には autobegin されたトランザクションが残ることがある
        # (seed の refresh など)。本番の get_session と同様に新しい
        # トランザクションを開始するため、ここで一度 commit しておく。
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_headers(TEST_USER_ID),
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient]:
    """管理者用 BFF プロキシ認証ヘッダーを付与済みの httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession]:
        # db_session には autobegin されたトランザクションが残ることがある
        # (seed の refresh など)。本番の get_session と同様に新しい
        # トランザクションを開始するため、ここで一度 commit しておく。
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_auth_headers(TEST_ADMIN_ID, role="admin"),
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def sample_categories(
    db_session: AsyncSession,
) -> list[Category]:
    """サンプルカテゴリを作成して返す (name は通常のカラム)。"""
    seed = [
        ("ai", "AI"),
        ("computing", "次世代コンピューティング"),
        ("semiconductor", "半導体"),
    ]
    categories: list[Category] = []
    for slug, name in seed:
        cat = Category(slug=slug, name=name)
        db_session.add(cat)
        categories.append(cat)
    await db_session.commit()
    for cat in categories:
        await db_session.refresh(cat)
    return categories


@pytest.fixture
async def sample_source(db_session: AsyncSession) -> NewsSource:
    """テスト用 RSS ニュースソースを作成して返す。"""
    source = NewsSource(
        name="Test Tech Source",
        source_type=SourceType.RSS,
        site_url="https://example.com",
        endpoint_url="https://example.com/feed.xml",
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def sample_hn_source(db_session: AsyncSession) -> NewsSource:
    """テスト用 Hacker News API ソースを作成して返す。"""
    source = NewsSource(
        name="Hacker News",
        source_type=SourceType.API,
        site_url="https://news.ycombinator.com",
        endpoint_url="https://hn.algolia.com/api/v1/search_by_date",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


@pytest.fixture
async def sample_av_source(db_session: AsyncSession) -> NewsSource:
    """テスト用 Alpha Vantage API ソースを作成して返す。"""
    source = NewsSource(
        name="Alpha Vantage",
        source_type=SourceType.API,
        site_url="https://www.alphavantage.co",
        endpoint_url="https://www.alphavantage.co/query",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source
