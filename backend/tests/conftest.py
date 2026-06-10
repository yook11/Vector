"""バックエンドテスト共通のフィクスチャ。"""

# ruff: noqa: E402
# 下記 ``os.environ.setdefault`` ブロックを app.* import より前に置く必要があるため、
# E402 (module-level import not at top of file) はファイル全体で抑止する。

# --- 起動時 dummy env 補完 (collect 段階の ImportError 回避) ---
# config.py は本番 fail-fast を維持するため required field に default を持たない
# (PR #405-407 / red-team S-AUTH-4 + S-SECRET-1 防御)。一方 ``settings = Settings()``
# は module load 時に走るため、.env も env も無い sandbox / agent 環境では
# conftest の ``from app.config import settings`` 行で ValidationError → pytest
# が collection 段階で全テストを諦める。
#
# 解決策: ``.env`` がプロジェクトルートに無い場合のみ、app.* の import より前
# (このブロック) で ``os.environ.setdefault`` で非機密 dummy 値を補う。
#
# 重要: 無条件に ``setdefault`` すると、``.env`` がある環境でも先に dummy が
# os.environ に焼き付き、pydantic-settings の優先順位 (env vars > .env) で
# ``.env`` の値が無視される。``.env`` 不在検知で gate しない限り、
# ``DATABASE_URL=postgresql+asyncpg://...@unreachable.invalid`` が実 DB 接続
# を奪い、integration テストが ``socket.gaierror`` で全件 ERROR になる。
#
# 設計制約:
# - ``.env`` の探索パスは ``app/config.py`` の ``_ENV_FILE`` と完全一致させる
#   (``Path(__file__).resolve().parent.parent.parent / ".env"``)。worktree でも
#   symlink などで ``.env`` を見えるようにすれば実 settings が走る。
# - BFF_JWT_SIGNING_SECRET / REVALIDATE_BEARER_SECRET は 32 chars 以上 +
#   ``_KNOWN_WEAK_INTERNAL_SECRETS`` ("secret" / "change-me*" 等) に該当せず、
#   互いに別値であること (Phase A.3 で 2 secret を必須化 / 同一値拒否)。
# - DATABASE_URL は ``_KNOWN_WEAK_DATABASE_URL_PATTERNS`` (vector_app:vector_app /
#   <set-strong-password) を含まないこと。
# - DATABASE_URL の host は意図的に到達不能 (`.invalid` は RFC 2606 予約 TLD)。
#   実 DB が要るテストは ``db_session`` 等の fixture で別経路で接続するため、
#   ここの dummy が偶発的に手元 Postgres へ接続してデータを汚染する事故を防ぐ。
import os
from pathlib import Path

_REPO_ROOT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"

if not _REPO_ROOT_ENV.exists():
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://test:test@unreachable.invalid:5432/none",
    )
    # BFF_JWT_SIGNING_SECRET / REVALIDATE_BEARER_SECRET は必須なので bootstrap でも
    # 両方設定する。2 値は同一値拒否を避けるため別値にする。
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

import time
from collections.abc import AsyncGenerator

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.dependencies import get_session
from app.main import app
from app.models import (  # noqa: F401
    Article,
    ArticleCuration,
    Category,
    InScopeAssessment,
    NewsSource,
    OutOfScopeAssessment,
    PipelineEvent,
    SourceType,
    WatchlistEntry,
    WeeklyBriefing,
)
from app.models.base import Base

# テスト用 DB は admin (migration role) で接続する: vector_test の create / drop、
# auth schema 作成、Base.metadata.create_all、seed user 投入は table owner
# の権限が必要なため、application role (vector_app) では実行できない。
# 権限境界の振る舞いは tests/test_db_user_isolation.py で別途 application role
# 接続を作って assert する。
_ADMIN_DB_URL = settings.migration_database_url or settings.database_url
TEST_DATABASE_URL = _ADMIN_DB_URL.rsplit("/", 1)[0] + "/vector_test"
engine_test = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

# --- BFF JWT auth helpers ---
# BFF (Next.js) は Better Auth セッションから user_id/role を取り出して
# HS256 JWT に署名し、backend に Authorization: Bearer で渡す。本ヘルパは
# テストで同じ secret を使って疑似 BFF として JWT を発行する。

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
    """fixture 依存から unit / integration マーカーを自動付与する。

    DB セッション・httpx Client・seed データなどの integration 用 fixture を
    要求するテストは integration、それ以外は unit として扱う。autouse の
    ensure_test_database / setup_db はパッケージ全体の前提なので無視する。

    既に手動で unit / integration marker を付けたテストは尊重し、自動付与を
    スキップする。integration fixture を介さず自前で実 DB へ接続する
    test_db_application_name のようなテストが、fixture 不使用ゆえに unit と
    誤分類され unit job (DB 無し) で接続失敗するのを防ぐ。marker のみを見る
    get_closest_marker を使い、node 名 (file / class / func) との衝突を避ける。
    """
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
    """vector_test DB・pgvector / auth スキーマ・全テーブルを確保する (初回のみ)。

    integration テスト初回起動時にだけ呼ばれる。DB / extension / schema の確保に
    加え、``Base.metadata`` の全テーブルを **session で 1 回だけ** 作成する。
    drop_all → create_all の順にするのは、``down -v`` せず再実行した persistent な
    local DB でも schema を fresh に揃えるための保険 (一回限りなのでコストは無視)。
    各テストごとの状態リセットは setup_db の TRUNCATE が担うため、create_all は
    session 中 1 度きりとなり per-test の DDL コストを排除する。

    unit テストのみの実行 (CI の backend-unit job 等、postgres service 無しの環境)
    ではそもそも呼ばれないため、DB 接続も発生しない。
    """
    global _test_schema_initialized
    if _test_schema_initialized:
        return
    base_url = _ADMIN_DB_URL.rsplit("/", 1)[0] + "/postgres"
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
    # auth スキーマ作成後にテーブルを 1 回だけ作る (auth."user" が auth schema に
    # 属するため順序を保つ)。
    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    _test_schema_initialized = True


def _truncate_all_sql() -> str:
    """全テーブルを 1 文で空にする TRUNCATE 文を組み立てる。

    ``Base.metadata.sorted_tables`` を走査し、dialect の identifier preparer で
    schema 修飾・予約語クォート (auth."user" 等) を安全に処理する。RESTART
    IDENTITY で採番 PK の sequence を 1 に戻し (seed fixture が ``.id`` に依存)、
    CASCADE は FK 参照に対する防御的指定。テーブル追加に自動追従するよう定数化
    せず都度生成する。
    """
    preparer = engine_test.dialect.identifier_preparer
    names = ", ".join(
        preparer.format_table(table) for table in Base.metadata.sorted_tables
    )
    return f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"


@pytest.fixture(autouse=True)
async def setup_db(request: pytest.FixtureRequest) -> AsyncGenerator[None]:
    """integration テストのみ、各テスト前に DB を空状態 + seed にリセットする。

    schema (テーブル) は _ensure_test_schema_once が session で 1 回だけ作成する。
    各テストの分離は create_all/drop_all ではなく TRUNCATE ... RESTART IDENTITY で
    行い、per-test の DDL コスト (~99ms) を排除する。リセットを setup (テスト前)
    に置くのは、前テストが異常終了でデータを残してもテスト順序に依らず必ず clean
    を保証するため。

    unit テスト (pytest_collection_modifyitems で自動分類) は DB を触らないため
    何もしない。
    """
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
