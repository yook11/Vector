"""取得処理のDB接続とRSSのHTTP応答を準備する。"""

from unittest.mock import AsyncMock

import pytest

from local_tests.http import StubHttp


@pytest.fixture
def rss_response(monkeypatch):
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


@pytest.fixture
def invoke_acquisition(system_database, monkeypatch):
    """取得Lambdaの接続設定と署名境界を準備する。"""
    import asyncio
    import json

    from app.collection.sources.acquisition_request import SourceAcquisitionSchedule
    from app.lambda_handlers import article_fetch_lifecycle
    from app.lambda_handlers.acquisition import handler as entrypoint
    from app.lambda_handlers.acquisition.handler import handler
    from tests.iam_fixtures import inject_test_db_signer

    url = inject_test_db_signer(
        monkeypatch,
        system_database.url("vector_collect", sqlalchemy=True),
        resources_module=article_fetch_lifecycle,
    )
    for key, value in {
        "ENV": "test",
        "DATABASE_URL": url,
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
        "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
        "CROSSREF_CONTACT_EMAIL": "crossref-contact@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(entrypoint, "setup_lambda_logging", lambda: None)
    schedule = SourceAcquisitionSchedule.model_validate(
        {"cadence": "high", "scheduled_at": "2026-09-15T00:00:00Z"}
    )

    async def invoke(source_id):
        event = {
            "Records": [
                {
                    "messageId": "acquisition-message",
                    "body": json.dumps(schedule.create_request(source_id).to_message()),
                }
            ]
        }
        return await asyncio.to_thread(handler, event, None)

    return invoke
