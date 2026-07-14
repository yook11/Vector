"""バックエンドテスト共通のフィクスチャ。"""

# ruff: noqa: E402
# E402 は app.* import 前に collect 用 dummy env を入れるため、ファイル単位で抑止する。

# .env 不在の sandbox でも collection が通るよう、app.* import 前に非機密 dummy を補う。
# .env がある環境では pydantic-settings の優先順位を壊さないよう補完しない。
# DATABASE_URL は .invalid に向け、integration fixture 外で実 DB へ誤接続しない。
import os
import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path

_REPO_ROOT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"

if not _REPO_ROOT_ENV.exists():
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://test:test@unreachable.invalid:5432/none",
    )
    # Settings の必須 secret 検証を満たすため、bootstrap 値も十分長く相互に別値にする。
    os.environ.setdefault(
        "BFF_JWT_SIGNING_SECRET",
        "test-only-collect-bootstrap-bff-xxxxxxxxxxxx",
    )
    os.environ.setdefault(
        "REVALIDATE_BEARER_SECRET",
        "test-only-collect-bootstrap-rev-xxxxxxxxxxxx",
    )
    os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")
    os.environ.setdefault("INTERNAL_FRONTEND_BASE_URL", "http://localhost:3000")
    os.environ.setdefault("CROSSREF_CONTACT_EMAIL", "crossref-contact@example.invalid")

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.compiler import DDLCompiler
from sqlalchemy.sql.ddl import ExecutableDDLElement

from app.config import settings
from app.dependencies import get_session
from app.main import app
from app.models import (  # noqa: F401
    AnalyzableArticleRecord,
    AnalyzedArticleRecord,
    ArticleCuration,
    Category,
    NewsSource,
    OutOfScopeArticleRecord,
    PipelineEvent,
    SourceType,
    WatchlistEntry,
    WeeklyBriefing,
)
from app.models.base import Base

_XDIST_WORKER_PATTERN = re.compile(r"gw\d+")


class _CreateDatabase(ExecutableDDLElement):
    inherit_cache = False

    def __init__(self, name: str) -> None:
        self.name = name


@compiles(_CreateDatabase, "postgresql")
def _compile_create_database(
    element: _CreateDatabase, compiler: DDLCompiler, **_: object
) -> str:
    return f"CREATE DATABASE {compiler.preparer.quote(element.name)}"


def _test_database_name_for_worker(worker_id: str | None) -> str:
    """xdist worker ごとに衝突しないテスト DB 名を返す。"""
    if worker_id in {None, "master"}:
        return "vector_test"
    if _XDIST_WORKER_PATTERN.fullmatch(worker_id) is None:
        raise ValueError(f"invalid pytest-xdist worker id: {worker_id!r}")
    return f"vector_test_{worker_id}"


# テスト DB 初期化は table owner 権限が必要なため migration role で接続する。
# application role の権限境界は tests/test_db_user_isolation.py が所有する。
_ADMIN_DB_URL = settings.migration_database_url or settings.database_url
TEST_DATABASE_NAME = _test_database_name_for_worker(
    os.environ.get("PYTEST_XDIST_WORKER")
)
TEST_DATABASE_URL = _ADMIN_DB_URL.rsplit("/", 1)[0] + f"/{TEST_DATABASE_NAME}"
# pytest-asyncio の function-scope loop 間で接続を再利用しない。
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

TEST_USER_ID = "00000000-0000-4000-a000-000000000001"
TEST_ADMIN_ID = "00000000-0000-4000-a000-000000000002"
INTERNAL_SECRET = settings.bff_jwt_signing_secret.get_secret_value()
_JWT_ALGORITHM = "HS256"
_JWT_TTL_SECONDS = 60


def make_internal_jwt(user_id: str, role: str = "user") -> str:
    """テスト用に BFF 模擬の HS256 JWT を発行する。

    iss / aud は backend (`app/dependencies.py`) と frontend
    (`frontend/src/lib/api/internal-config.ts`) で揃える必要がある。
    """
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "iss": "vector-bff",
            "aud": "vector-backend",
            "iat": now,
            "exp": now + _JWT_TTL_SECONDS,
        },
        INTERNAL_SECRET,
        algorithm=_JWT_ALGORITHM,
    )


def _auth_headers(user_id: str, role: str = "user") -> dict[str, str]:
    """テスト用 Authorization: Bearer <jwt> ヘッダを組み立てる。"""
    return {"Authorization": f"Bearer {make_internal_jwt(user_id, role)}"}


def make_bff_jwt() -> str:
    """user-less な BFF 経由証明 JWT を発行する (sub/role 無し)。

    本番 frontend の ``buildBffRequestHeaders`` と対称で、iss/aud/exp/iat のみ
    署名する。require_bff_request は通すが get_current_user / get_admin_user は
    sub/role 欠落で 401 になる、という非対称をテストするための fixture。
    """
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "vector-bff",
            "aud": "vector-backend",
            "iat": now,
            "exp": now + _JWT_TTL_SECONDS,
        },
        INTERNAL_SECRET,
        algorithm=_JWT_ALGORITHM,
    )


def _bff_headers() -> dict[str, str]:
    """user-less BFF 経由証明ヘッダを組み立てる。"""
    return {"Authorization": f"Bearer {make_bff_jwt()}"}


_INTEGRATION_FIXTURES = frozenset(
    {
        "db_session",
        "client",
        "bff_client",
        "authed_client",
        "admin_client",
        "session_factory",
        "test_database_url",
        "sample_categories",
        "sample_source",
        "sample_hn_source",
        "sample_av_source",
        # test_db_user_isolation.py が直接 asyncpg.connect する権限境界テスト
        # 用 fixture。実 Postgres + vector_auth / vector_app / vector_collect role
        # を要求するため必ず integration 側に分類する。
        "auth_conn",
        "app_conn",
        "collect_conn",
    }
)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """fixture 依存から unit / integration マーカーを自動付与する。"""
    for item in items:
        if item.get_closest_marker("integration") or item.get_closest_marker("unit"):
            continue
        fixtures = set(getattr(item, "fixturenames", ()))
        if fixtures & _INTEGRATION_FIXTURES:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)


_test_schema_initialized = False


async def _ensure_test_schema_once() -> None:
    """worker の integration 初回だけ DB・schema・全テーブルを確保する。"""
    global _test_schema_initialized
    if _test_schema_initialized:
        return
    base_url = _ADMIN_DB_URL.rsplit("/", 1)[0] + "/postgres"
    engine = create_async_engine(
        base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
            {"database_name": TEST_DATABASE_NAME},
        )
        if not result.scalar():
            await conn.execute(_CreateDatabase(TEST_DATABASE_NAME))
    await engine.dispose()

    async with engine_test.connect() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
        await conn.commit()
    # auth スキーマ作成後にテーブルを 1 回だけ作る (auth."user" が auth schema に
    # 属するため順序を保つ)。
    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    _test_schema_initialized = True


def _truncate_all_sql() -> str:
    """全テーブルを 1 文で空にする TRUNCATE 文を metadata から組み立てる。"""
    preparer = engine_test.dialect.identifier_preparer
    names = ", ".join(
        preparer.format_table(table) for table in Base.metadata.sorted_tables
    )
    return f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"


@pytest.fixture(autouse=True)
async def setup_db(request: pytest.FixtureRequest) -> AsyncGenerator[None]:
    """integration テストだけ、各テスト前に DB を空状態 + seed にリセットする。"""
    if "integration" not in request.keywords:
        yield
        return
    await _ensure_test_schema_once()
    async with engine_test.begin() as conn:
        await conn.execute(text(_truncate_all_sql()))
        # watchlist_entries.user_id の FK を満たすため auth.user を seed する
        await conn.execute(
            text(
                'INSERT INTO auth."user" (id) VALUES (:uid1), (:uid2) '
                "ON CONFLICT DO NOTHING"
            ),
            {"uid1": TEST_USER_ID, "uid2": TEST_ADMIN_ID},
        )
    yield


@pytest.fixture
def test_database_url() -> str:
    """現在の pytest worker 専用テスト DB URL を返す。"""
    return TEST_DATABASE_URL


@pytest.fixture
def session_factory() -> async_sessionmaker[AsyncSession]:
    """Service クラスのテスト用に session factory を提供する。"""
    return async_sessionmaker(
        engine_test,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """テスト用 DB セッションを提供する。"""
    async with AsyncSession(engine_test, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """DI でセッションを差し替えた httpx AsyncClient を提供する。"""

    async def override_session() -> AsyncGenerator[AsyncSession]:
        # 本番の get_session と同様に新しいトランザクションを開始する。
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
        # 本番の get_session と同様に新しいトランザクションを開始する。
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
        # 本番の get_session と同様に新しいトランザクションを開始する。
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
def bff_headers() -> dict[str, str]:
    """user-less BFF 経由証明ヘッダーを返す (sub/role 無し)。"""
    return _bff_headers()


@pytest.fixture
async def bff_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient]:
    """user-less BFF 経由証明ヘッダーを付与済みの httpx AsyncClient を提供する。

    require_bff_request を満たす共有 read endpoint 用。user 非依存なので sub/role
    を持たず、watchlist / admin など get_current_user 系では 401 になる。
    """

    async def override_session() -> AsyncGenerator[AsyncSession]:
        if db_session.in_transaction():
            await db_session.commit()
        async with db_session.begin():
            yield db_session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_bff_headers(),
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
