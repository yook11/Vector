"""実Lambda入口・IAM接続・本体・SDK送信境界を接続する。"""

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from hashlib import md5
from unittest.mock import Mock

import pytest

from app.lambda_handlers.assessment.event import parse_curated_signal_event
from app.lambda_handlers.backfill import resources
from app.lambda_handlers.curation.event import parse_analyzable_article_created_event
from app.lambda_handlers.embedding.event import parse_assessed_in_scope_event
from app.outbox.sqs import publisher
from tests.backfill.helpers import seed_target
from tests.iam_fixtures import inject_test_db_signer

handler = importlib.import_module("app.lambda_handlers.backfill.handler")
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "stage,plural,parse",
    [
        ("curation", "curations", parse_analyzable_article_created_event),
        ("assessment", "assessments", parse_curated_signal_event),
        ("embedding", "embeddings", parse_assessed_in_scope_event),
    ],
)
async def test_handler_replays_persisted_fact_with_fresh_invocation_resources(
    monkeypatch,
    test_database_url,
    db_session,
    sample_source,
    sample_categories,
    stage,
    plural,
    parse,
):
    """繰り返した実入口が接続を持ち越さず、同じ未完了事実を新IDで配送する。"""
    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    target = await seed_target(
        db_session, sample_source, sample_categories[0], stage, now - timedelta(hours=1)
    )
    database_url = inject_test_db_signer(
        monkeypatch, test_database_url, resources_module=resources
    )
    for name, value in {
        "ENV": "test",
        "DATABASE_URL": database_url,
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
        f"SQS_ARTICLE_{stage.upper()}_QUEUE_URL": f"https://sqs.invalid/{stage}",
        f"BACKFILL_{plural.upper()}_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(handler, "utc_now", lambda: now)
    batches, clients, engines, loops = [], [], [], []
    create_engine = resources.create_backfill_engine

    def track_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        engines.append(engine)
        loops.append(asyncio.get_running_loop())
        return engine

    monkeypatch.setattr(resources, "create_backfill_engine", track_engine)

    def send(**request):
        batches.append(request)
        return {
            "Successful": [
                {
                    "Id": entry["Id"],
                    "MessageId": "accepted",
                    "MD5OfMessageBody": md5(
                        entry["MessageBody"].encode(), usedforsecurity=False
                    ).hexdigest(),
                }
                for entry in request["Entries"]
            ]
        }

    def create_client(**kwargs):
        client = Mock(send_message_batch=Mock(side_effect=send))
        clients.append(client)
        return client

    monkeypatch.setattr(publisher, "create_sqs_client", create_client)
    entry = getattr(handler, f"{stage}_handler")
    for _ in range(2):
        assert await asyncio.to_thread(entry, {}, None) is None

    events = [parse(batch["Entries"][0]["MessageBody"]) for batch in batches]
    expected = {
        "curation": {"analyzable_article_id": target.article_id},
        "assessment": {
            "analyzable_article_id": target.article_id,
            "curation_id": target.curation_id,
        },
        "embedding": {
            "curation_id": target.curation_id,
            "analyzed_article_id": target.target_id,
        },
    }[stage]
    assert len(events) == 2
    assert events[0].event_id != events[1].event_id
    for batch, event in zip(batches, events, strict=True):
        assert batch["QueueUrl"] == f"https://sqs.invalid/{stage}"
        assert event.payload.model_dump() == expected
        assert event.occurred_at == target.occurred_at
        assert json.loads(batch["Entries"][0]["MessageBody"])["event_id"] == str(
            event.event_id
        )
    assert engines[0] is not engines[1]
    assert loops[0] is not loops[1]
    assert all(loop.is_closed() for loop in loops)
    assert all(engine.pool.checkedout() == 0 for engine in engines)
    for client in clients:
        client.close.assert_called_once()
