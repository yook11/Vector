"""補完テストの設定・接続・Consumer・HTTP応答と待機・確定制御を組み立てる。"""

from contextlib import asynccontextmanager
from functools import partial
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text

from app.db.engine import create_worker_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.outbox_relay.settings import OutboxRelayConnectionSettings
from local_tests.completion.commit_control import hold_commit
from local_tests.completion.http_control import GatedResponses
from local_tests.completion.support import article_response, consumer_contract
from local_tests.http import StubHttp


@pytest.fixture
def first_read_failure_sessions(completion_sessions):
    first_read = True

    @asynccontextmanager
    async def open_session():
        nonlocal first_read
        async with completion_sessions() as session:
            if first_read:
                first_read = False
                await session.execute(text("SELECT 1 / 0"))
            yield session

    return open_session


@pytest.fixture
def http_clients(monkeypatch, http_boundary):
    from app.collection.article_completion import article_fetch

    clients = []

    def create_client(**kwargs):
        client = http_boundary.create_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(article_fetch, "make_external_async_client", create_client)
    return clients


@pytest.fixture
async def completion_engine(system_database):
    engine = create_worker_engine(
        OutboxRelayConnectionSettings(
            env="test",
            database_url=system_database.url("vector_collect", sqlalchemy=True),
            db_iam_auth=False,
            aws_region="ap-northeast-1",
        ),
        "collection",
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def completion_sessions(completion_engine):
    return caller_managed_session_factory(completion_engine)


@pytest.fixture
def completion_consumer(completion_sessions, completion_settings):
    return consumer_contract().ArticleCompletionConsumer(completion_sessions)


@pytest.fixture
def page_response():
    return AsyncMock(return_value=article_response())


@pytest.fixture
def completion_settings(monkeypatch, system_database):
    # 補完packageの旧経路importが要求する設定にはテスト専用値だけを渡す。
    for name, value in {
        "ENV": "development",
        "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
        "DATABASE_URL": system_database.url("vector_collect", sqlalchemy=True),
        "DB_IAM_AUTH": "false",
        "BFF_JWT_SIGNING_SECRET": "test-completion-bff-xxxxxxxxxxxx",
        "REVALIDATE_BEARER_SECRET": "test-completion-rev-xxxxxxxxxxxx",
        "FRONTEND_URL": "http://localhost:3000",
        "INTERNAL_FRONTEND_BASE_URL": "http://localhost:3000",
        "CROSSREF_CONTACT_EMAIL": "crossref-contact@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def http_boundary(monkeypatch, completion_settings, page_response):
    from app.collection.article_completion import article_fetch

    async def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return await page_response(request)

    http = StubHttp(respond)
    monkeypatch.setattr(article_fetch, "make_external_async_client", http.create_client)
    return http


@pytest.fixture
def gated_pages(page_response):
    responses = GatedResponses()
    page_response.side_effect = responses.respond
    try:
        yield responses.register
    finally:
        responses.release_all()


@pytest.fixture
def control_commit(monkeypatch):
    return partial(hold_commit, monkeypatch)
