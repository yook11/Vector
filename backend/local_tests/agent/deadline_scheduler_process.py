"""期限回収テストで本番のagent用スケジューラーだけを起動する。"""

import asyncio
import sys

from local_tests.agent.worker_process import load_isolated_settings


async def main():
    settings = load_isolated_settings()

    from redis.asyncio import Redis

    from app.queue.scheduler_entrypoint import _run_one
    from app.queue.schedulers import scheduler_agent
    from local_tests.agent.worker_support import WorkerControl

    async with Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=None
    ) as redis:
        control = WorkerControl(redis, sys.argv[1])
        async with asyncio.TaskGroup() as tasks:
            scheduling = tasks.create_task(_run_one(scheduler_agent))
            stopping = tasks.create_task(
                redis.blpop(f"agent-test:{control.name}:stop", timeout=0)
            )
            await control.send("events", {"kind": "ready"})
            done, _ = await asyncio.wait(
                (scheduling, stopping), return_when=asyncio.FIRST_COMPLETED
            )
            if scheduling in done:
                stopping.cancel()
                raise RuntimeError("停止要求より前にスケジューラーが終了しました")
            scheduling.cancel()


if __name__ == "__main__":
    asyncio.run(main())
