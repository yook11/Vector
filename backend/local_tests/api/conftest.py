"""製品のAPIをvector_apiのEngineで動かし、BFFと同じ形式の証明を付けて呼ぶ。"""

import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.agent.live_updates.sse import AgentRunSseCapacity
from app.agent.live_updates.transport import (
    AgentLiveTransport,
    get_agent_live_transport,
)
from app.agent.running.deadline.scheduling import get_agent_deadline_scheduler
from app.agent.runs.enqueuer import get_agent_run_enqueuer
from app.db.engine import create_api_engine
from app.db.session import caller_managed_session_factory
from local_tests.api.research_boundaries import (
    RecordingDeadlineScheduler,
    RecordingLiveRedis,
    RecordingRunEnqueuer,
)


@pytest.fixture
def api_settings(monkeypatch, system_database):
    # app.mainのimportが要求する設定には、テスト専用値だけを渡す。
    for name, value in {
        "ENV": "development",
        "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
        "DATABASE_URL": system_database.url("vector_api", sqlalchemy=True),
        "DB_IAM_AUTH": "false",
        "BFF_JWT_SIGNING_SECRET": "test-api-bff-signing-key-xxxxxxxxxxxx",
        "REVALIDATE_BEARER_SECRET": "test-api-revalidate-xxxxxxxxxxxxxxx",
        "FRONTEND_URL": "http://localhost:3000",
        "INTERNAL_FRONTEND_BASE_URL": "http://localhost:3000",
        "CROSSREF_CONTACT_EMAIL": "crossref-contact@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
async def api_engine(system_database):
    engine = create_api_engine(
        SimpleNamespace(
            database_url=system_database.url("vector_api", sqlalchemy=True),
            db_iam_auth=False,
            aws_region=None,
        )
    )
    try:
        async with engine.connect() as connection:
            role = await connection.scalar(text("SELECT current_user"))
        if role != "vector_api":
            raise RuntimeError(f"API試験がvector_api以外で接続している: {role}")
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def api_session(api_engine):
    """観測時刻を固定して集計の規則を確かめるため、serviceへ直接渡すセッション。"""
    async with caller_managed_session_factory(api_engine)() as session:
        yield session


@pytest.fixture
async def api_client(api_settings, api_engine, monkeypatch):
    from app.main import app

    # lifespanは走らせず、本番と同じ2つの入口にvector_apiのEngineを渡す。
    monkeypatch.setattr(app.state, "engine", api_engine, raising=False)
    monkeypatch.setattr(
        app.state,
        "session_factory",
        caller_managed_session_factory(api_engine),
        raising=False,
    )
    try:
        async with httpx.AsyncClient(  # noqa: TID251
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sign_as_bff(api_settings):
    """製品が検証に使う鍵で、BFFと同じ形式の証明を作る。"""
    from app.config import settings

    # 先に別の試験がsettingsを作っていても、検証と同じ鍵で署名する。
    key = settings.bff_jwt_signing_secret.get_secret_value()

    def sign(claims: dict[str, str]) -> dict[str, str]:
        now = int(time.time())
        token = jwt.encode(
            {
                **claims,
                "iss": "vector-bff",
                "aud": "vector-backend",
                "iat": now,
                "exp": now + 60,
            },
            key,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    return sign


@pytest.fixture
def bff_headers(sign_as_bff):
    """利用者を含まないBFF経由の証明（閲覧系が要求する）を付ける。"""
    return sign_as_bff({})


@pytest.fixture
async def user_id(system_database):
    """Better Authの利用者表に、一般の利用者を1人用意する。"""
    user_id = uuid4()
    async with system_database.connect("vector") as connection:
        await connection.execute(
            'INSERT INTO auth."user" '
            '(id, name, email, "emailVerified", "createdAt", "updatedAt", role) '
            "VALUES ($1, 'API test', 'api@example.test', false, now(), now(), 'user')",
            user_id,
        )
    return user_id


@pytest.fixture
def user_headers(user_id, sign_as_bff):
    """その利用者としてBFFが署名した証明を付ける。"""
    return sign_as_bff({"sub": str(user_id), "role": "user"})


@pytest.fixture
def live_redis():
    return RecordingLiveRedis()


@pytest.fixture
def run_enqueuer():
    return RecordingRunEnqueuer()


@pytest.fixture
def deadline_scheduler():
    return RecordingDeadlineScheduler()


@pytest.fixture
def research_client(
    api_client, live_redis, run_enqueuer, deadline_scheduler, monkeypatch
):
    """Redis側の依存を記録用に差し替え、リサーチのAPIを呼べる状態にする。"""
    from app.agent.router import get_agent_run_sse_capacity
    from app.config import settings
    from app.main import app

    # 開始APIは外部検索の設定があることだけを確かめ、実際には呼ばない。
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("test-deepseek-key"))
    monkeypatch.setattr(
        settings, "agentcore_gateway_url", "https://gateway.example.test"
    )
    transport = AgentLiveTransport(live_redis)
    capacity = AgentRunSseCapacity()
    app.dependency_overrides.update(
        {
            get_agent_live_transport: lambda: transport,
            get_agent_run_enqueuer: lambda: run_enqueuer,
            get_agent_deadline_scheduler: lambda: deadline_scheduler,
            get_agent_run_sse_capacity: lambda: capacity,
        }
    )
    return api_client


@pytest.fixture
def admin_headers(sign_as_bff):
    """管理者としてBFFが署名した証明を付ける。管理の操作は利用者の行を参照しない。"""
    return sign_as_bff({"sub": str(uuid4()), "role": "admin"})
