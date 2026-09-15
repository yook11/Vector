"""取得処理のDB接続とRSSのHTTP応答を準備する。"""

from unittest.mock import AsyncMock

import pytest

from app.db.engine import create_worker_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.outbox_relay.settings import OutboxRelayConnectionSettings
from local_tests.http import StubHttp


@pytest.fixture
async def acquisition_sessions(system_database, monkeypatch):
    """旧取得経路の設定にはテスト値を渡し、Collect接続を管理する。"""
    database_url = system_database.url("vector_collect", sqlalchemy=True)
    for name, value in {
        "ENV": "development",
        "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
        "DATABASE_URL": database_url,
        "DB_IAM_AUTH": "false",
        "BFF_JWT_SIGNING_SECRET": "test-curation-delivery-bff-xxxxxxxxxxxx",
        "REVALIDATE_BEARER_SECRET": "test-curation-delivery-rev-xxxxxxxxxxxx",
        "FRONTEND_URL": "http://localhost:3000",
        "INTERNAL_FRONTEND_BASE_URL": "http://localhost:3000",
        "CROSSREF_CONTACT_EMAIL": "crossref-contact@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    engine = create_worker_engine(
        OutboxRelayConnectionSettings(
            env="test",
            database_url=database_url,
            db_iam_auth=False,
            aws_region="ap-northeast-1",
        ),
        "collection",
    )
    try:
        yield caller_managed_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def acquisition_service(acquisition_sessions):
    from app.collection.article_acquisition.service import ArticleAcquisitionService
    from app.collection.sources.definitions.venturebeat import VentureBeatSource

    return ArticleAcquisitionService(acquisition_sessions, VentureBeatSource())


@pytest.fixture
def rss_response(acquisition_sessions, monkeypatch):
    from app.collection.article_acquisition.reader import rss_reader

    response = AsyncMock()
    http = StubHttp(response)
    monkeypatch.setattr(rss_reader, "make_external_async_client", http.create_client)
    return response


@pytest.fixture
async def source_id(system_database):
    async with system_database.connect("vector_app") as reader:
        source_id = await reader.fetchval(
            "SELECT id FROM news_sources WHERE name=$1", "VentureBeat"
        )
    if source_id is None:
        raise AssertionError("VentureBeat source is required for acquisition tests")
    return source_id
