# ruff: noqa: S101
"""migration適用済みDBで、記事完成からCuration確定までを接続する。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from importlib import import_module
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.ai_providers.gemini import client as gemini_module
from app.db.engine import create_worker_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers import article_analysis_lifecycle as lifecycle
from app.lambda_handlers.outbox_relay import curation_handler
from app.lambda_handlers.outbox_relay.settings import OutboxRelayConnectionSettings
from tests.iam_fixtures import inject_test_db_signer
from tests.outbox.curation_runtime import CURATION_QUEUE_URL, configure_curation_relay

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def acquisition_sessions(system_database, monkeypatch):
    """旧取得経路の全体設定には非機密のテスト値だけを渡す。"""
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


async def test_both_article_creation_paths_reach_curation_through_stored_events(
    system_database, acquisition_sessions, monkeypatch
):
    """両経路の記事完成を同じ保存済みイベントで届け、対応するCurationを確定する。"""
    from app.collection.article_acquisition.reader import rss_reader
    from app.collection.article_acquisition.service import ArticleAcquisitionService
    from app.collection.article_completion.ready import ReadyForArticleCompletion
    from app.collection.article_completion.repository import ArticleCompletionRepository
    from app.collection.article_completion.scraper import ArticleScraper, ScrapedContent
    from app.collection.article_completion.service import ArticleCompletionService
    from app.collection.domain.value_objects import PublishedAt
    from app.collection.sources.definitions.venturebeat import VentureBeatSource

    feed = (
        """<rss version="2.0"><channel><title>VentureBeat</title>
    <item><title>Acquired article</title><link>https://venturebeat.com/delivery-acquired</link>
    <pubDate>Sun, 13 Sep 2026 00:00:00 GMT</pubDate><description>"""
        + "Acquired content. " * 20
        + """</description></item>
    <item><title>Completed article</title><link>https://venturebeat.com/delivery-completed</link>
    <pubDate>Sun, 13 Sep 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    )
    monkeypatch.setattr(
        rss_reader,
        "make_external_async_client",
        lambda **kwargs: httpx.AsyncClient(  # noqa: TID251
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=feed)
            ),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        ArticleScraper,
        "scrape",
        AsyncMock(
            return_value=ScrapedContent(
                title="Completed article",
                body="Completed content. " * 20,
                published_at=PublishedAt(datetime(2026, 9, 13, tzinfo=UTC)),
            )
        ),
    )
    async with system_database.connect("vector_app") as reader:
        source_id = await reader.fetchval(
            "SELECT id FROM news_sources WHERE name=$1", "VentureBeat"
        )
    assert source_id is not None
    acquired_ids = await ArticleAcquisitionService(
        acquisition_sessions, VentureBeatSource()
    ).execute(source_id)
    async with acquisition_sessions() as session:
        repository = ArticleCompletionRepository(session)
        now = datetime.now(UTC)
        pending_ids = await repository.claim_ready_batch(
            limit=10, now=now, leased_until=now + timedelta(minutes=5)
        )
        await session.commit()
        assert len(pending_ids) == 1
        ready = await ReadyForArticleCompletion.try_advance_from(
            incomplete_article_id=pending_ids[0], repo=repository
        )
    completed_id = await ArticleCompletionService(acquisition_sessions).execute(ready)
    assert len(acquired_ids) == 1 and completed_id is not None
    article_ids = {*acquired_ids, completed_id}
    assert len(article_ids) == 2

    async with system_database.connect("vector_app") as reader:
        before = await reader.fetch(
            "SELECT event_id, event_type, schema_version, occurred_at, payload "
            "FROM outbox_events"
        )
        upstream_audits = await reader.fetch(
            "SELECT article_id FROM pipeline_events "
            "WHERE event_type='succeeded' AND article_id IS NOT NULL"
        )
    assert {row["article_id"] for row in upstream_audits} == article_ids
    assert sorted(row["event_type"] for row in before) == [
        "article.analyzable_created",
        "article.analyzable_created",
        "article.incomplete_recorded",
    ]
    created = {
        row["event_id"]: row
        for row in before
        if row["event_type"] == "article.analyzable_created"
    }
    assert [row["schema_version"] for row in created.values()] == [1, 1]
    assert {tuple(json.loads(row["payload"]).items()) for row in created.values()} == {
        (("analyzable_article_id", article_id),) for article_id in article_ids
    }

    relay = configure_curation_relay(
        monkeypatch, system_database.url("vector_app", sqlalchemy=True)
    )
    await asyncio.to_thread(curation_handler, {}, None)
    assert [batch["QueueUrl"] for batch in relay.batches] == [CURATION_QUEUE_URL]
    entries = relay.batches[0]["Entries"]
    assert {UUID(entry["Id"]) for entry in entries} == set(created)
    for entry in entries:
        body = json.loads(entry["MessageBody"])
        saved = created[UUID(entry["Id"])]
        assert body == {
            "event_id": str(saved["event_id"]),
            "event_type": saved["event_type"],
            "schema_version": saved["schema_version"],
            "occurred_at": saved["occurred_at"].isoformat().replace("+00:00", "Z"),
            "payload": json.loads(saved["payload"]),
        }
    async with system_database.connect("vector_app") as reader:
        published = await reader.fetch(
            "SELECT event_id FROM outbox_events WHERE published_at IS NOT NULL"
        )
    assert {row["event_id"] for row in published} == set(created)

    inject_test_db_signer(
        monkeypatch,
        system_database.url("vector_app", sqlalchemy=True),
        resources_module=lifecycle,
    )
    monkeypatch.setenv("GEMINI_API_KEY_PARAMETER_PATH", "/test/curation/key")
    monkeypatch.setattr(
        lifecycle, "get_secret_parameter", Mock(return_value=SecretStr("test-key"))
    )
    monkeypatch.setattr(
        import_module("app.lambda_handlers.curation.handler"),
        "setup_lambda_logging",
        Mock(),
    )

    def ai_response(request):
        content = request.content.decode()
        if "Acquired content." in content:
            title, summary = "取得記事", "取得本文の要約"
        else:
            assert "Completed content." in content
            title, summary = "補完記事", "補完本文の要約"
        body = json.dumps(
            {"relevance": "signal", "title_ja": title, "summary_ja": summary}
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": body}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    def http_factory(**kwargs):
        kwargs.pop("retries")
        return httpx.AsyncClient(transport=httpx.MockTransport(ai_response), **kwargs)  # noqa: TID251

    monkeypatch.setattr(gemini_module, "make_external_async_client", http_factory)
    records = [
        {"messageId": f"sqs-{entry['Id']}", "body": entry["MessageBody"]}
        for entry in entries
    ]
    response = await asyncio.to_thread(
        import_module("app.lambda_handlers.curation.handler").handler,
        {"Records": records},
        None,
    )
    assert response == {"batchItemFailures": []}
    async with system_database.connect("vector_app") as reader:
        curations = await reader.fetch(
            "SELECT id, analyzable_article_id, translated_title, summary "
            "FROM article_curations"
        )
        audits = await reader.fetch(
            "SELECT article_id FROM pipeline_events "
            "WHERE stage='curation' AND event_type='succeeded'"
        )
        downstream = await reader.fetch(
            "SELECT payload FROM outbox_events "
            "WHERE event_type='article.curated_signal'"
        )
    assert {
        row["analyzable_article_id"]: (row["translated_title"], row["summary"])
        for row in curations
    } == {
        acquired_ids[0]: ("取得記事", "取得本文の要約"),
        completed_id: ("補完記事", "補完本文の要約"),
    }
    assert len(audits) == 2 and {row["article_id"] for row in audits} == article_ids
    assert len(downstream) == 2
    assert {
        tuple(sorted(json.loads(row["payload"]).items())) for row in downstream
    } == {
        tuple(
            sorted(
                {
                    "curation_id": row["id"],
                    "analyzable_article_id": row["analyzable_article_id"],
                }.items()
            )
        )
        for row in curations
    }
