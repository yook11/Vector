"""ワーカー死亡後の期限予約・別プロセスによる回収を観測する。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.agent.running.creation import AgentRunCreationRepository
from app.agent.running.deadline.scheduling import AgentDeadlineScheduler
from app.queue.deadline_schedule import create_deadline_schedule_source

RECOVERY_WAIT_SECONDS = 120


@dataclass(frozen=True)
class ObservedDeadlineCheck:
    worker_pid: int
    observed_at: datetime
    result: dict


class WorkerDeadlineRecovery:
    def __init__(self, workers):
        self.workers = workers
        self.control = workers.controls[1]

    async def create_run(self, *, user_id, question, accepted_ago=timedelta()):
        async with self.workers.owner_session_factory() as session:
            async with session.begin():
                database_time = await session.scalar(select(func.clock_timestamp()))
                created = await AgentRunCreationRepository(session).create_user_run(
                    user_id=user_id,
                    question=question,
                    thread_id=None,
                    now=database_time - accepted_ago,
                )
        source = create_deadline_schedule_source(self.workers.settings)
        try:
            await source.startup()
            await AgentDeadlineScheduler(source).reserve(
                created.run_id, created.deadline_at
            )
        finally:
            await source.shutdown()
        return created

    async def start(self):
        await self.control.send("receive", {"continuous": True})
        await self.workers.start_process(
            "scheduler", "local_tests.agent.deadline_scheduler_process"
        )

    async def _observe_check(self, created):
        event = await self.control.receive("events", timeout=RECOVERY_WAIT_SECONDS)
        if event["kind"] != "delivery_completed" or event["is_err"]:
            raise RuntimeError(f"期限回収ワーカーの配送処理が失敗しました: {event}")
        if event["task_name"] not in {
            "check_agent_run_deadline",
            "sweep_deadline_exceeded_agent_runs",
        }:
            raise RuntimeError(f"期限確認以外のタスクを受信しました: {event}")
        if event["worker_pid"] != self.workers.processes[self.control.name].pid:
            raise RuntimeError("期限確認を実行したプロセスが一致していません")
        result = await self.workers.observe_run(
            run_id=created.run_id, thread_id=created.thread_id
        )
        async with self.workers.owner_session_factory() as session:
            observed_at = await session.scalar(select(func.clock_timestamp()))
        return event, ObservedDeadlineCheck(event["worker_pid"], observed_at, result)

    async def wait_for_original_deadline_check(self, created):
        async with asyncio.timeout(RECOVERY_WAIT_SECONDS):
            while True:
                event, observed = await self._observe_check(created)
                if (
                    event["task_name"] == "check_agent_run_deadline"
                    and event["run_id"] == str(created.run_id)
                    and observed.observed_at >= created.deadline_at
                ):
                    return observed

    async def wait_for_recovery(self, created):
        async with asyncio.timeout(RECOVERY_WAIT_SECONDS):
            while True:
                _, observed = await self._observe_check(created)
                if observed.result["status"] == "deadline_exceeded":
                    return observed
