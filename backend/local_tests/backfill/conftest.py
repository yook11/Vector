"""backfillの実接続と外部SDK境界を用意する。"""

import asyncio
from dataclasses import dataclass, field
from hashlib import md5
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.lambda_handlers.backfill import resources
from app.lambda_handlers.backfill.settings import CurationBackfillSettings
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.route import EventDeliveryRoute
from app.outbox.sqs import publisher
from tests.iam_fixtures import inject_test_db_signer


@pytest.fixture
def backfill_connection(system_database, monkeypatch):
    url = inject_test_db_signer(
        monkeypatch,
        system_database.url("vector_backfill", sqlalchemy=True),
        resources_module=resources,
    )
    for key, value in {
        "ENV": "test",
        "DATABASE_URL": url,
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
    }.items():
        monkeypatch.setenv(key, value)
    return url


@pytest.fixture
def delivery(backfill_connection, monkeypatch):
    from local_tests.backfill.support import NOW

    handler = import_module("app.lambda_handlers.backfill.handler")
    monkeypatch.setattr(handler, "utc_now", lambda: NOW)
    for stage, plural in [
        ("curation", "curations"),
        ("assessment", "assessments"),
        ("embedding", "embeddings"),
        ("completion", "completions"),
    ]:
        monkeypatch.setenv(
            f"SQS_ARTICLE_{stage.upper()}_QUEUE_URL", f"https://sqs.invalid/{stage}"
        )
        monkeypatch.setenv(f"BACKFILL_{plural.upper()}_ENABLED", "true")
    batches = []

    def send(**request):
        batches.append(request)
        return {
            "Successful": [
                {
                    "Id": item["Id"],
                    "MessageId": "accepted",
                    "MD5OfMessageBody": md5(
                        item["MessageBody"].encode(), usedforsecurity=False
                    ).hexdigest(),
                }
                for item in request["Entries"]
            ]
        }

    monkeypatch.setattr(
        publisher,
        "create_sqs_client",
        lambda **kwargs: Mock(send_message_batch=Mock(side_effect=send)),
    )
    return SimpleNamespace(handler=handler, batches=batches)


@dataclass
class Invocation:
    rds: object
    engine: AsyncEngine | None = None
    pids: set[int] = field(default_factory=set)
    order: list[str] = field(default_factory=list)
    disposed: bool = False
    loop: object = None


@pytest.fixture
def lifecycle(backfill_connection, monkeypatch):
    settings = CurationBackfillSettings(
        sqs_article_curation_queue_url="https://sqs.invalid/curation"
    )
    route = EventDeliveryRoute(
        "article.analyzable_created",
        settings.sqs_article_curation_queue_url,
        build_analyzable_created_message,
    )
    invocations = []
    sdk = resources.Session()

    def create_rds(*args, **kwargs):
        rds = sdk.create_client(*args, **kwargs)
        scope = Invocation(rds)
        invocations.append(scope)
        close = rds.close

        def close_rds():
            close()
            scope.order.append("rds")

        rds.close = close_rds
        return rds

    monkeypatch.setattr(
        resources, "Session", lambda: SimpleNamespace(create_client=create_rds)
    )
    create_engine = resources.create_backfill_engine

    def observe_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        scope = invocations[-1]
        scope.engine = engine
        scope.loop = asyncio.get_running_loop()

        @event.listens_for(engine.sync_engine, "connect")
        def connected(connection, record):
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT pg_backend_pid()")
                scope.pids.add(cursor.fetchone()[0])
            finally:
                cursor.close()

        return engine

    monkeypatch.setattr(resources, "create_backfill_engine", observe_engine)
    dispose = AsyncEngine.dispose

    async def observe_dispose(engine, *args, **kwargs):
        await dispose(engine, *args, **kwargs)
        scope = next(item for item in invocations if item.engine is engine)
        scope.disposed = True
        scope.order.append("engine")

    monkeypatch.setattr(AsyncEngine, "dispose", observe_dispose)
    return SimpleNamespace(settings=settings, route=route, invocations=invocations)
