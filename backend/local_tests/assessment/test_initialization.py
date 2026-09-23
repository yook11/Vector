"""実DBのカテゴリー整合性が、記事処理を開始する条件になることを確認する。"""

import pytest

from app.analysis.assessment.repository import CategoryEnumDatabaseMismatchError
from app.db.errors import DatabaseUnexpectedError
from local_tests.assessment.support import (
    fetch_stored_assessment,
    invoke_event,
    seed_curation,
)


@pytest.mark.asyncio
async def test_missing_unused_category_prevents_article_processing(
    system_database,
    assessment_runtime,
    deepseek_response,
    notification_response,
):
    """AIが使う予定のないカテゴリーの不足でも、記事処理を開始しない。"""
    target = await seed_curation(
        system_database, "https://example.com/missing-category"
    )
    async with system_database.connect("vector") as connection:
        await connection.execute("DELETE FROM categories WHERE slug='computing'")

    with pytest.raises(CategoryEnumDatabaseMismatchError) as caught:
        await invoke_event(target)

    assert caught.value.missing == {"computing"}
    deepseek_response.assert_not_awaited()
    notification_response.assert_not_awaited()
    saved = await fetch_stored_assessment(system_database, target.curation_id)
    assert saved.in_scope == saved.out_of_scope == saved.audits == saved.outbox == []


@pytest.mark.asyncio
async def test_catalog_read_failure_prevents_article_processing(
    system_database, assessment_runtime, deepseek_response, notification_response
):
    """カテゴリーを読めないDB障害でも、記事処理を開始しない。"""
    target = await seed_curation(
        system_database, "https://example.com/catalog-db-error"
    )
    async with system_database.connect("vector") as connection:
        await connection.execute(
            "REVOKE SELECT ON categories FROM vector_article_analysis"
        )

    with pytest.raises(DatabaseUnexpectedError):
        await invoke_event(target)

    deepseek_response.assert_not_awaited()
    notification_response.assert_not_awaited()
    async with system_database.connect("vector") as connection:
        counts = await connection.fetchrow(
            "SELECT "
            "(SELECT count(*) FROM analyzed_articles WHERE curation_id=$1), "
            "(SELECT count(*) FROM out_of_scope_articles WHERE curation_id=$1), "
            "(SELECT count(*) FROM pipeline_events WHERE stage='assessment' "
            "AND (payload->>'curation_id')::integer=$1), "
            "(SELECT count(*) FROM outbox_events "
            "WHERE event_type='article.assessed_in_scope' "
            "AND (payload->>'curation_id')::integer=$1)",
            target.curation_id,
        )
    assert tuple(counts) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_next_invocation_rechecks_catalog_after_success(
    system_database, assessment_runtime, deepseek_response, notification_response
):
    """前回の検証成功をキャッシュせず、次の呼び出しでも不足を検知する。"""
    target = await seed_curation(system_database, "https://example.com/recheck-catalog")
    assert await invoke_event(target) == {"batchItemFailures": []}
    deepseek_response.reset_mock()
    notification_response.reset_mock()
    async with system_database.connect("vector") as connection:
        await connection.execute("DELETE FROM categories WHERE slug='computing'")

    with pytest.raises(CategoryEnumDatabaseMismatchError) as caught:
        await invoke_event(target)

    assert caught.value.missing == {"computing"}
    deepseek_response.assert_not_awaited()
    notification_response.assert_not_awaited()
