"""保存後・ACK前に死亡したワーカーの配送を回収しても確定回答を維持する。"""

import pytest

from local_tests.agent.support import create_user_run, fetch_saved_run_result


@pytest.mark.asyncio
async def test_redelivery_after_worker_death_preserves_committed_answer(
    agent_workers,
    agent_user_id,
):
    """回答保存後・ACK前にワーカーが死亡しても、再生成せず確定回答を維持する。"""
    created = await create_user_run(
        agent_workers.owner_session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )

    await agent_workers.enqueue(created.run_id, pause_before_ack=True)
    initial_execution = await agent_workers.wait_for_answer_request()
    await initial_execution.respond("売上は前年同期比10%増です。[[1]]")

    pending_delivery = await initial_execution.wait_until_before_ack()
    result_after_initial_execution = await fetch_saved_run_result(
        agent_workers.database,
        run_id=created.run_id,
        thread_id=created.thread_id,
    )

    await initial_execution.kill()

    # 新規投入せず、死亡したワーカーの未ACK配送を別ワーカーが回収する。
    redelivery = await agent_workers.recover_unacknowledged_delivery(pending_delivery)

    result_after_redelivery = await fetch_saved_run_result(
        agent_workers.database,
        run_id=created.run_id,
        thread_id=created.thread_id,
    )

    assert redelivery.worker_pid != initial_execution.worker_pid
    assert redelivery.stream_message_id == pending_delivery.stream_message_id
    assert redelivery.task_id == initial_execution.task_id
    assert redelivery.answer_generation_calls == 0
    assert redelivery.acknowledged is True
    assert result_after_redelivery == result_after_initial_execution
