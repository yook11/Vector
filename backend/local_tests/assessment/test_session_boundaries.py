"""実ConsumerがAI待機前に読み取りセッションを返すことを確認する。"""

import asyncio

import pytest

from local_tests.assessment.support import deepseek_reply, invoke_event, seed_curation


@pytest.mark.asyncio
async def test_read_session_is_returned_while_waiting_for_ai(
    system_database, assessment_runtime, analysis_engines, gated_ai_responses
):
    """AI応答の待機中は、貸出接続も実DBのトランザクションも残らない。"""
    target = await seed_curation(system_database, "https://example.com/ai-wait")
    gate = gated_ai_responses(deepseek_reply())
    invocation = asyncio.create_task(invoke_event(target))
    try:
        await gate.wait_requested()
        assert len(analysis_engines) == 1
        assert analysis_engines[0].pool.checkedout() == 0
        assert not invocation.done()
        async with system_database.connect("vector_app") as connection:
            rows = await connection.fetch(
                "SELECT state, xact_start FROM pg_stat_activity "
                "WHERE datname=current_database() AND application_name=$1",
                "vector-assessment-consumer",
            )
        assert len(rows) == 1
        assert rows[0]["state"] == "idle"
        assert rows[0]["xact_start"] is None
    finally:
        gate.release()
        await asyncio.wait_for(asyncio.shield(invocation), 15)
