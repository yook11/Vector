"""各工程の実入口から、migration適用済みDBの事実を配送する。"""

import asyncio

import pytest

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.analysis.curation.events import ArticleCuratedSignalEvent
from app.collection.article_acquisition.events import IncompleteArticleRecordedEvent
from app.collection.events import AnalyzableArticleCreatedEvent
from app.lambda_handlers.sqs.records import SqsRecord
from local_tests.backfill.support import (
    ANALYZED_AT,
    CREATED_AT,
    EXTRACTED_AT,
    seed_analysis,
    seed_article,
    seed_curation,
    seed_incomplete_article,
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
    event = AnalyzableArticleCreatedEvent.from_input(
        SqsRecord(message_id="id", body=batch["Entries"][0]["MessageBody"]).parse_json()
    )
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
    event = ArticleCuratedSignalEvent.from_input(
        SqsRecord(message_id="id", body=batch["Entries"][0]["MessageBody"]).parse_json()
    )
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
    event = ArticleAssessedInScopeEvent.from_input(
        SqsRecord(message_id="id", body=batch["Entries"][0]["MessageBody"]).parse_json()
    )
    assert event.payload.model_dump() == {
        "curation_id": curation_id,
        "analyzed_article_id": analyzed_id,
    }
    assert event.occurred_at == ANALYZED_AT


@pytest.mark.asyncio
async def test_completion_delivers_saved_incomplete_article(system_database, delivery):
    """補完キューにclosedでない未完成行の保存事実が配送される。"""
    incomplete_id = await seed_incomplete_article(
        system_database, "https://example.com/completion"
    )
    await asyncio.to_thread(delivery.handler.completion_handler, {}, None)
    assert len(delivery.batches) == 1
    batch = delivery.batches[0]
    assert batch["QueueUrl"] == "https://sqs.invalid/completion"
    assert len(batch["Entries"]) == 1
    event = IncompleteArticleRecordedEvent.from_input(
        SqsRecord(message_id="id", body=batch["Entries"][0]["MessageBody"]).parse_json()
    )
    assert event.payload.incomplete_article_id == incomplete_id
    assert event.payload.source_id > 0
    assert event.occurred_at == CREATED_AT


@pytest.mark.asyncio
async def test_next_invocation_replays_unfinished_article(system_database, delivery):
    """未完了の同じ記事は次の起動でも新しいイベントIDで配送される。"""
    article_id = await seed_article(system_database, "https://example.com/replay")
    await asyncio.to_thread(delivery.handler.curation_handler, {}, None)
    first = AnalyzableArticleCreatedEvent.from_input(
        SqsRecord(
            message_id="id", body=delivery.batches[0]["Entries"][0]["MessageBody"]
        ).parse_json()
    )
    await asyncio.to_thread(delivery.handler.curation_handler, {}, None)
    assert len(delivery.batches) == 2
    second = AnalyzableArticleCreatedEvent.from_input(
        SqsRecord(
            message_id="id", body=delivery.batches[1]["Entries"][0]["MessageBody"]
        ).parse_json()
    )
    assert first.event_id != second.event_id
    assert (
        first.payload.analyzable_article_id
        == second.payload.analyzable_article_id
        == article_id
    )
