"""各工程の実入口から、migration適用済みDBの事実を配送する。"""

import asyncio

import pytest

from app.lambda_handlers.assessment.event import parse_curated_signal_event
from app.lambda_handlers.curation.event import parse_analyzable_article_created_event
from app.lambda_handlers.embedding.event import parse_assessed_in_scope_event
from local_tests.backfill.support import (
    ANALYZED_AT,
    CREATED_AT,
    EXTRACTED_AT,
    seed_analysis,
    seed_article,
    seed_curation,
)


@pytest.mark.asyncio
async def test_curation_delivers_saved_article(system_database, delivery):
    """curationキューに元記事IDと保存時刻が配送される。"""
    article_id = await seed_article(system_database, "https://example.com/curation")
    await asyncio.to_thread(delivery.handler.curation_handler, {}, None)
    assert len(delivery.batches) == 1
    batch = delivery.batches[0]
    assert batch["QueueUrl"] == "https://sqs.invalid/curation"
    assert len(batch["Entries"]) == 1
    event = parse_analyzable_article_created_event(batch["Entries"][0]["MessageBody"])
    assert event.payload.model_dump() == {"analyzable_article_id": article_id}
    assert event.occurred_at == CREATED_AT


@pytest.mark.asyncio
async def test_assessment_delivers_saved_curation(system_database, delivery):
    """assessmentキューにcurationの確定事実が配送される。"""
    article_id = await seed_article(system_database, "https://example.com/assessment")
    curation_id = await seed_curation(system_database, article_id)
    await asyncio.to_thread(delivery.handler.assessment_handler, {}, None)
    assert len(delivery.batches) == 1
    batch = delivery.batches[0]
    assert batch["QueueUrl"] == "https://sqs.invalid/assessment"
    assert len(batch["Entries"]) == 1
    event = parse_curated_signal_event(batch["Entries"][0]["MessageBody"])
    assert event.payload.model_dump() == {
        "analyzable_article_id": article_id,
        "curation_id": curation_id,
    }
    assert event.occurred_at == EXTRACTED_AT


@pytest.mark.asyncio
async def test_embedding_delivers_saved_assessment(system_database, delivery):
    """embeddingキューに対象内判定の確定事実が配送される。"""
    article_id = await seed_article(system_database, "https://example.com/embedding")
    curation_id = await seed_curation(system_database, article_id)
    analyzed_id = await seed_analysis(system_database, curation_id)
    await asyncio.to_thread(delivery.handler.embedding_handler, {}, None)
    assert len(delivery.batches) == 1
    batch = delivery.batches[0]
    assert batch["QueueUrl"] == "https://sqs.invalid/embedding"
    assert len(batch["Entries"]) == 1
    event = parse_assessed_in_scope_event(batch["Entries"][0]["MessageBody"])
    assert event.payload.model_dump() == {
        "curation_id": curation_id,
        "analyzed_article_id": analyzed_id,
    }
    assert event.occurred_at == ANALYZED_AT


@pytest.mark.asyncio
async def test_next_invocation_replays_unfinished_article(system_database, delivery):
    """未完了の同じ記事は次の起動でも新しいイベントIDで配送される。"""
    article_id = await seed_article(system_database, "https://example.com/replay")
    await asyncio.to_thread(delivery.handler.curation_handler, {}, None)
    first = parse_analyzable_article_created_event(
        delivery.batches[0]["Entries"][0]["MessageBody"]
    )
    await asyncio.to_thread(delivery.handler.curation_handler, {}, None)
    assert len(delivery.batches) == 2
    second = parse_analyzable_article_created_event(
        delivery.batches[1]["Entries"][0]["MessageBody"]
    )
    assert first.event_id != second.event_id
    assert (
        first.payload.analyzable_article_id
        == second.payload.analyzable_article_id
        == article_id
    )
