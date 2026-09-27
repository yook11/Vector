"""新着配送に依存せず、死亡したワーカーの未ACK配送を回収する。"""

import pytest

from local_tests.agent.support import create_user_run


@pytest.mark.asyncio
async def test_unacknowledged_delivery_is_recovered_without_new_messages(
    agent_workers,
    agent_user_id,
):
    """新しい配送がなくても、死亡したワーカーの未ACK配送を回収できる。"""
    created = await create_user_run(
        agent_workers.session_factory,
        user_id=agent_user_id,
        question="売上動向を調べてください",
    )

    await agent_workers.enqueue(created.run_id, pause_before_ack=True)
    initial_execution = await agent_workers.wait_for_answer_request()
    await initial_execution.respond("売上は前年同期比10%増です。[[1]]")
    pending_delivery = await initial_execution.wait_until_before_ack()

    await initial_execution.kill()

    # 空タスクも追加せず、Bの通常の受信・未ACK回収を開始する。
    await agent_workers.start_unacknowledged_recovery()
    redelivery = await agent_workers.wait_for_recovered_delivery(pending_delivery)

    assert redelivery.worker_pid != initial_execution.worker_pid
    assert redelivery.stream_message_id == pending_delivery.stream_message_id
    assert redelivery.task_id == initial_execution.task_id
    assert redelivery.acknowledged is True
    assert await agent_workers.count_stream_entries() == 1
