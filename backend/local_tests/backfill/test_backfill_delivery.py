"""各工程の実入口から、期限切れの整理と保存済み事実の配送を1回の実行で確定する。"""

import asyncio

import pytest

from app.analysis.assessment.events import ArticleAssessedInScopeEvent
from app.analysis.curation.events import ArticleCuratedSignalEvent
from app.collection.article_acquisition.events import IncompleteArticleRecordedEvent
from app.collection.events import AnalyzableArticleCreatedEvent
from app.lambda_handlers.sqs.records import SqsRecord
from local_tests.backfill.support import (
    AGED_CREATED_AT,
    ANALYZED_AT,
    CREATED_AT,
    EXTRACTED_AT,
    read_audit_events,
    seed_analysis,
    seed_article,
    seed_curation,
    seed_incomplete_article,
)


@pytest.mark.asyncio
async def test_curation_deletes_aged_out_article_and_delivers_saved_article(
    system_database, delivery
):
    """期限切れの未curation記事を削除して監査し、期間内の記事だけを配送する。"""
    aged_id = await seed_article(
        system_database, "https://example.com/curation-aged", AGED_CREATED_AT
    )
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

    async with system_database.connect("vector") as connection:
        assert not await connection.fetchval(
            "SELECT EXISTS (SELECT FROM analyzable_articles WHERE id=$1)", aged_id
        )
    audits = await read_audit_events(system_database)
    assert [audit[:4] for audit in audits] == [
        ("backfill_curate", "rejected", "backfill_curation_aged_out", None),
        ("backfill_curate", "succeeded", "backfill_item_enqueued", article_id),
    ]
    assert audits[0].payload["target_article_id"] == aged_id
    assert audits[1].payload["target_id"] == article_id


@pytest.mark.asyncio
async def test_assessment_excludes_aged_out_curation_and_delivers_saved_curation(
    system_database, delivery
):
    """期限切れの未判定curationを除外して監査し、期間内のcurationだけを配送する。"""
    aged_article_id = await seed_article(
        system_database, "https://example.com/assessment-aged", AGED_CREATED_AT
    )
    aged_curation_id = await seed_curation(system_database, aged_article_id)
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

    async with system_database.connect("vector") as connection:
        exclusions = await connection.fetch(
            "SELECT curation_id, reason_code FROM assessment_backfill_exclusions"
        )
    assert [tuple(row) for row in exclusions] == [
        (aged_curation_id, "backfill_assessment_aged_out")
    ]
    audits = await read_audit_events(system_database)
    assert [audit[:4] for audit in audits] == [
        (
            "backfill_assess",
            "rejected",
            "backfill_assessment_aged_out",
            aged_article_id,
        ),
        ("backfill_assess", "succeeded", "backfill_item_enqueued", article_id),
    ]
    assert audits[0].payload["curation_id"] == aged_curation_id
    assert audits[1].payload["target_id"] == curation_id


@pytest.mark.asyncio
async def test_embedding_excludes_aged_out_analysis_and_delivers_saved_assessment(
    system_database, delivery
):
    """期限切れのembedding未保存の分析結果を除外して監査し、期間内だけを配送する。"""
    aged_article_id = await seed_article(
        system_database, "https://example.com/embedding-aged", AGED_CREATED_AT
    )
    aged_curation_id = await seed_curation(system_database, aged_article_id)
    aged_analyzed_id = await seed_analysis(system_database, aged_curation_id)
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

    async with system_database.connect("vector") as connection:
        exclusions = await connection.fetch(
            "SELECT analyzed_article_id, reason_code FROM embedding_backfill_exclusions"
        )
    assert [tuple(row) for row in exclusions] == [
        (aged_analyzed_id, "backfill_embedding_aged_out")
    ]
    audits = await read_audit_events(system_database)
    assert [audit[:4] for audit in audits] == [
        ("backfill_embed", "rejected", "backfill_embedding_aged_out", aged_article_id),
        ("backfill_embed", "succeeded", "backfill_item_enqueued", article_id),
    ]
    assert audits[0].payload["analyzed_article_id"] == aged_analyzed_id
    assert audits[1].payload["target_id"] == analyzed_id


@pytest.mark.asyncio
async def test_completion_closes_aged_out_row_and_delivers_saved_incomplete_article(
    system_database, delivery
):
    """期限切れの未完成行をclosedにして監査し、期間内の未完成行だけを配送する。"""
    aged_id = await seed_incomplete_article(
        system_database, "https://example.com/completion-aged", AGED_CREATED_AT
    )
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

    async with system_database.connect("vector") as connection:
        closed = await connection.fetchrow(
            "SELECT status, leased_until FROM incomplete_articles WHERE id=$1", aged_id
        )
    assert tuple(closed) == ("closed", None)
    audits = await read_audit_events(system_database)
    assert [audit[:4] for audit in audits] == [
        ("completion", "rejected", "backfill_completion_aged_out", None),
        ("completion", "succeeded", "backfill_item_enqueued", None),
    ]
    assert audits[0].payload["incomplete_article_id"] == aged_id
    assert audits[1].payload["target_id"] == incomplete_id


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
