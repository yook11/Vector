"""同じ記事の補完を競合させ、指定した側から実DBへの確定を進める。"""

import asyncio

from local_tests.completion.support import wait_until_blocked


async def run_completion_race(
    target,
    *,
    system_database,
    first_consumer,
    second_consumer,
    gated_pages,
    control_commit,
    first_response,
    second_response,
    first_commit_phase,
):
    """両方のHTTP取得後に実ロック待ちを確認し、先行側の確定を再開する。"""
    first = gated_pages(first_response)
    second = gated_pages(second_response)
    tasks = []
    with control_commit(target, phase=first_commit_phase) as hold:
        try:
            tasks.append(asyncio.create_task(first_consumer.consume(target.id)))
            await first.wait_requested()
            tasks.append(asyncio.create_task(second_consumer.consume(target.id)))
            await second.wait_requested()
            first.release()
            await hold.wait_reached()
            second.release()
            await wait_until_blocked(system_database, hold.pid, tasks)
            hold.release()
            results = await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in tasks)), 15
            )
        finally:
            first.release()
            second.release()
            hold.release()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)

    return results
