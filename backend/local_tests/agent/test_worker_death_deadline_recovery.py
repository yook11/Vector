"""回答担当のプロセスが死亡しても、別ワーカーが生成段階に応じた期限で回収する。"""

from datetime import timedelta

import pytest


@pytest.mark.asyncio
async def test_worker_death_before_answer_generation_is_recovered(
    agent_workers, worker_deadline_recovery, agent_user_id
):
    """回答生成前に担当ワーカーが死亡しても、別のワーカーがランを回収できる。"""
    created = await worker_deadline_recovery.create_run(
        user_id=agent_user_id, question="売上動向を調べてください"
    )
    await agent_workers.enqueue(created.run_id)
    execution = await agent_workers.wait_for_planning_request()

    await execution.kill()
    await worker_deadline_recovery.start()
    recovered = await worker_deadline_recovery.wait_for_recovery(created)

    assert recovered.worker_pid != execution.worker_pid
    assert recovered.observed_at >= created.deadline_at
    assert recovered.result == {
        "status": "deadline_exceeded",
        "attempt_epoch": execution.attempt_epoch,
        "answers": [],
        "sources": [],
        "terminal_events": [
            {
                "attempt_epoch": execution.attempt_epoch,
                "status": "deadline_exceeded",
            }
        ],
    }


@pytest.mark.asyncio
async def test_worker_death_during_generation_is_recovered_without_final_answer(
    agent_workers, worker_deadline_recovery, agent_user_id
):
    """回答生成中に担当ワーカーが死亡した場合、生成用の期限で回収でき、生成中だった回答は確定されない。"""
    # 元の期限が生成用の期限より先に来るよう、受付から時間が経過したランを用意する。
    created = await worker_deadline_recovery.create_run(
        user_id=agent_user_id,
        question="売上動向を調べてください",
        accepted_ago=timedelta(seconds=40),
    )
    await agent_workers.enqueue(created.run_id)
    execution = await agent_workers.wait_for_answer_request()
    await execution.stream_partial_answer("売上動向について調査したところ、")

    await execution.kill()
    await worker_deadline_recovery.start()

    original_deadline_check = (
        await worker_deadline_recovery.wait_for_original_deadline_check(created)
    )
    assert original_deadline_check.observed_at < execution.answer_recovery_deadline
    assert original_deadline_check.result == {
        "status": "running",
        "attempt_epoch": execution.attempt_epoch,
        "answers": [],
        "sources": [],
        "terminal_events": [],
    }

    recovered = await worker_deadline_recovery.wait_for_recovery(created)

    assert recovered.worker_pid != execution.worker_pid
    assert recovered.observed_at >= execution.answer_recovery_deadline
    assert recovered.result == {
        "status": "deadline_exceeded",
        "attempt_epoch": execution.attempt_epoch,
        "answers": [],
        "sources": [],
        "terminal_events": [
            {
                "attempt_epoch": execution.attempt_epoch,
                "status": "deadline_exceeded",
            }
        ],
    }
