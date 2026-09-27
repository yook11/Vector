"""重複配送のタイミングに応じて、実行権を持つワーカーだけが回答を確定する。"""

import pytest

from local_tests.agent.support import create_user_run


@pytest.mark.asyncio
async def test_duplicate_before_answer_generation_only_latest_execution_saves_answer(
    agent_workers, agent_user_id
):
    """回答生成前に重複投入された場合、古い世代は回答生成に進まず最新世代だけが確定する。"""
    created = await create_user_run(
        agent_workers.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    latest_answer = "最新世代の回答です。売上は前年同期比10%増です。[[1]]"

    await agent_workers.enqueue(created.run_id)
    older_execution = await agent_workers.wait_for_planning_request()
    await agent_workers.enqueue(created.run_id)
    latest_execution = await agent_workers.wait_for_planning_request()

    assert older_execution.worker_pid != latest_execution.worker_pid
    assert older_execution.attempt_epoch < latest_execution.attempt_epoch

    # 両世代とも回答生成前に止め、世代の交代によって古い処理が拒否されることを確認する。
    await older_execution.resume_planning()
    older_execution_result = await older_execution.wait_for_answer_generation_stop()
    assert older_execution_result == {
        "stop_reason": "not_current",
        "completion_returned": False,
    }

    result_after_older_execution = await agent_workers.observe_run(
        run_id=created.run_id, thread_id=created.thread_id
    )
    assert result_after_older_execution == {
        "status": "running",
        "attempt_epoch": latest_execution.attempt_epoch,
        "answers": [],
        "sources": [],
        "terminal_events": [],
    }

    await latest_execution.resume_planning()
    await latest_execution.wait_for_answer_request()
    await latest_execution.respond(latest_answer)
    await latest_execution.wait_for_delivery_completion()

    result_after_latest_execution = await agent_workers.observe_run(
        run_id=created.run_id, thread_id=created.thread_id
    )
    assert result_after_latest_execution == {
        "status": "completed",
        "attempt_epoch": latest_execution.attempt_epoch,
        "answers": [latest_answer],
        "sources": [
            {
                "source_ref": "1",
                "url": "https://example.com/initial-report",
            }
        ],
        "terminal_events": [
            {
                "attempt_epoch": latest_execution.attempt_epoch,
                "status": "completed",
            }
        ],
    }


@pytest.mark.asyncio
async def test_duplicate_during_generation_preserves_initial_execution(
    agent_workers, agent_user_id
):
    """回答生成中の重複配送は再実行せず、先行ワーカーの回答が確定する。"""
    created = await create_user_run(
        agent_workers.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )
    initial_answer = "売上は前年同期比10%増です。[[1]]"

    await agent_workers.enqueue(created.run_id)
    initial_execution = await agent_workers.wait_for_answer_request()

    result_during_initial_execution = await agent_workers.observe_run(
        run_id=created.run_id, thread_id=created.thread_id
    )

    await agent_workers.enqueue(created.run_id)
    duplicate_delivery = await agent_workers.wait_for_skipped_delivery()

    result_after_duplicate_delivery = await agent_workers.observe_run(
        run_id=created.run_id, thread_id=created.thread_id
    )
    assert duplicate_delivery.worker_pid != initial_execution.worker_pid
    assert duplicate_delivery.answer_generation_calls == 0
    assert duplicate_delivery.acknowledged is True
    assert result_after_duplicate_delivery == result_during_initial_execution

    await initial_execution.respond(initial_answer)
    await initial_execution.wait_for_delivery_completion()

    saved_result = await agent_workers.observe_run(
        run_id=created.run_id, thread_id=created.thread_id
    )
    assert saved_result == {
        "status": "completed",
        "attempt_epoch": initial_execution.attempt_epoch,
        "answers": [initial_answer],
        "sources": [
            {
                "source_ref": "1",
                "url": "https://example.com/initial-report",
            }
        ],
        "terminal_events": [
            {
                "attempt_epoch": initial_execution.attempt_epoch,
                "status": "completed",
            }
        ],
    }
