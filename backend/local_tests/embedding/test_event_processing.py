"""Embeddingイベントの保存結果と処理結果の整合性を確認する。"""

import json

import httpx
import pytest

from local_tests.embedding.support import invoke_event, seed_article


@pytest.mark.asyncio
async def test_event_saves_embedding_for_the_target_article(
    system_database, embedding_runtime, gemini_response
):
    """生成可能な分析済み記事2件のうち、イベントで指定した記事だけにAI応答を保存する。"""
    other_article = await seed_article(
        system_database,
        "https://example.com/other-article",
        title="イベントで指定していない記事のタイトル",
        summary="イベントで指定していない記事固有の本文",
    )
    target = await seed_article(
        system_database,
        "https://example.com/target",
        title="対象記事のタイトル",
        summary="対象記事固有の本文",
    )
    expected_vector = [(index - 384) / 512 for index in range(768)]
    sent_ai_request_bodies = []

    async def respond(request):
        sent_ai_request_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [{"values": expected_vector}]})

    gemini_response.side_effect = respond
    response = await invoke_event(target)

    assert response == {"batchItemFailures": []}
    assert len(sent_ai_request_bodies) == 1
    embedding_requests = sent_ai_request_bodies[0]["requests"]
    assert len(embedding_requests) == 1
    sent_text = "\n".join(
        part["text"]
        for part in embedding_requests[0]["content"]["parts"]
        if "text" in part
    )
    assert "対象記事固有の本文" in sent_text
    assert "イベントで指定していない記事固有の本文" not in sent_text

    async with system_database.connect("vector_app") as connection:
        rows = await connection.fetch(
            "SELECT id, embedding::text AS embedding "
            "FROM analyzed_articles WHERE id=ANY($1::integer[])",
            [target.analyzed_article_id, other_article.analyzed_article_id],
        )
    stored = {row["id"]: row["embedding"] for row in rows}
    assert set(stored) == {
        target.analyzed_article_id,
        other_article.analyzed_article_id,
    }
    assert stored[target.analyzed_article_id] is not None
    assert json.loads(stored[target.analyzed_article_id]) == pytest.approx(
        expected_vector, abs=0.001
    )
    assert stored[other_article.analyzed_article_id] is None


@pytest.mark.asyncio
async def test_event_rolls_back_embedding_and_reports_failure_on_database_error(
    system_database, embedding_runtime, database_error_after_update
):
    """保存途中でDBエラーが発生したら更新がロールバックされ、イベントの処理結果も失敗になる。"""
    target = await seed_article(system_database, "https://example.com/save-failure")

    response = await invoke_event(target)

    assert database_error_after_update.updated_before_error == [
        (target.analyzed_article_id, True, True)
    ]
    assert len(database_error_after_update.database_errors) == 1
    assert response == {
        "batchItemFailures": [{"itemIdentifier": str(target.analyzed_article_id)}]
    }
    async with system_database.connect("vector_app") as connection:
        row = await connection.fetchrow(
            "SELECT embedding FROM analyzed_articles WHERE id = $1",
            target.analyzed_article_id,
        )
    assert row is not None
    assert row["embedding"] is None
