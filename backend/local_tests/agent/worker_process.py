"""本番のTaskiq受信・ワーカー処理を通し、外部AIの応答だけを親から受け取る。"""

import asyncio
import os
import sys
from contextlib import ExitStack, aclosing, asynccontextmanager
from unittest.mock import patch
from uuid import UUID

from pydantic_settings import BaseSettings


def load_isolated_settings():
    original_init = BaseSettings.__init__

    def without_dotenv(self, *args, **kwargs):
        original_init(self, *args, **{**kwargs, "_env_file": None})

    # 子プロセスは明示したテスト環境だけを使い、共有の.envを読み込まない。
    with patch.object(BaseSettings, "__init__", without_dotenv):
        from app.config import settings
    return settings


async def main():
    settings = load_isolated_settings()

    from redis.asyncio import Redis
    from sqlalchemy import select
    from taskiq import TaskiqMiddleware
    from taskiq.acks import AcknowledgeType
    from taskiq.receiver import Receiver

    from app.agent import composition
    from app.agent.evidence_collection.external_search.contract import ExternalSearchHit
    from app.agent.evidence_collection.external_search.service import (
        ExternalSearchService,
    )
    from app.agent.evidence_collection.internal_search.ai import gemini
    from app.agent.running.answer_generation import AgentAnswerGenerationRepository
    from app.agent.running.completion import AgentRunCompletionRepository
    from app.agent.runs.execution import Stop
    from app.models.agent_run import AgentRun
    from app.queue.brokers import broker_agent
    from local_tests.agent.support import AgentProviderResponses
    from local_tests.agent.worker_support import WorkerControl

    async with Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=None
    ) as redis:
        control = WorkerControl(redis, sys.argv[1])
        delivery = None
        completion_returned = False
        answer_generation_stop_reason = None
        answer_generation_calls = 0
        stream_message_id = None
        acknowledged = False
        pause_before_ack = False
        processed = asyncio.Event()
        finish = asyncio.Event()
        original_listen = broker_agent.listen
        original_complete = AgentRunCompletionRepository.complete_run
        original_answer_start = AgentAnswerGenerationRepository.start_answer_generation
        original_ack_generator = broker_agent._ack_generator

        def observe_ack(id, queue_name):
            original_ack = original_ack_generator(id, queue_name)

            async def acknowledge():
                nonlocal stream_message_id, acknowledged
                stream_message_id = id.decode() if isinstance(id, bytes) else id
                if pause_before_ack:
                    run_id = UUID(delivery.kwargs["trigger"]["run_id"])
                    async with broker_agent.state.session_factory() as session:
                        run = await session.get(AgentRun, run_id)
                        if (
                            run is None
                            or run.status != "completed"
                            or run.assistant_message_id is None
                            or not completion_returned
                        ):
                            raise RuntimeError("回答のcommit前にACKへ到達しました")
                    await control.send(
                        "events",
                        {
                            "kind": "before_ack",
                            "task_id": delivery.task_id,
                            "stream_message_id": stream_message_id,
                        },
                    )
                    await control.receive("ack")
                await original_ack()
                pending = await redis.xpending_range(
                    queue_name, "taskiq", stream_message_id, stream_message_id, 1
                )
                acknowledged = not pending

            return acknowledge

        async def receive_deliveries():
            nonlocal delivery, pause_before_ack, completion_returned
            nonlocal answer_generation_calls, answer_generation_stop_reason
            nonlocal stream_message_id, acknowledged
            instruction = await control.receive("receive")
            pause_before_ack = instruction.get("pause_before_ack", False)
            if instruction.get("recover_pending", False):
                # 死亡確認後の回収だけ待機を省き、XAUTOCLAIM自体は実brokerへ任せる。
                broker_agent.idle_timeout = 0
                await control.send(
                    "events",
                    {"kind": "recovery_started", "worker_pid": os.getpid()},
                )
            async with aclosing(original_listen()) as messages:
                async for message in messages:
                    delivery = broker_agent.formatter.loads(message=message.data)
                    completion_returned = False
                    answer_generation_calls = 0
                    answer_generation_stop_reason = None
                    stream_message_id = None
                    acknowledged = False
                    processed.clear()
                    yield message
                    await processed.wait()
                    if not instruction["continuous"]:
                        # 競合テストでは先読みで次の配送まで奪わせない。
                        await finish.wait()
                        return

        async def observe_completion(repository, **kwargs):
            nonlocal completion_returned
            result = await original_complete(repository, **kwargs)
            completion_returned = True
            return result

        async def observe_answer_start(repository, **kwargs):
            nonlocal answer_generation_stop_reason, answer_generation_calls
            answer_generation_calls += 1
            result = await original_answer_start(repository, **kwargs)
            if isinstance(result, Stop):
                answer_generation_stop_reason = result.reason.value
            return result

        class DeliveryObserver(TaskiqMiddleware):
            async def post_execute(self, message, result):
                trigger = message.kwargs.get("trigger")
                if trigger is None and message.args:
                    trigger = message.args[0]
                await control.send(
                    "events",
                    {
                        "kind": "delivery_completed",
                        "task_id": message.task_id,
                        "task_name": message.task_name,
                        "run_id": str(trigger.run_id) if trigger is not None else None,
                        "worker_pid": os.getpid(),
                        "is_err": result.is_err,
                        "completion_returned": completion_returned,
                        "answer_generation_stop_reason": answer_generation_stop_reason,
                        "answer_generation_calls": answer_generation_calls,
                        "stream_message_id": stream_message_id,
                        "consumer_name": broker_agent.consumer_name,
                        "acknowledged": acknowledged,
                    },
                )
                processed.set()

        class ControlledResponses(AgentProviderResponses):
            async def report_request(self, kind):
                if delivery is None:
                    raise RuntimeError("配送の受信前にAIが呼ばれました")
                run_id = UUID(delivery.kwargs["trigger"]["run_id"])
                async with broker_agent.state.session_factory() as session:
                    epoch, answer_started_at = (
                        await session.execute(
                            select(
                                AgentRun.attempt_epoch, AgentRun.answer_started_at
                            ).where(AgentRun.id == run_id)
                        )
                    ).one()
                await control.send(
                    "events",
                    {
                        "kind": kind,
                        "run_id": str(run_id),
                        "task_id": delivery.task_id,
                        "worker_pid": os.getpid(),
                        "consumer_name": broker_agent.consumer_name,
                        "attempt_epoch": epoch,
                        "answer_started_at": (
                            answer_started_at.isoformat() if answer_started_at else None
                        ),
                    },
                )

            async def call(self, agent, input, *, attempt_number):
                if agent.name == "question_planner":
                    await self.report_request("planning_requested")
                    await control.receive("planning")
                return await super().call(agent, input, attempt_number=attempt_number)

            async def stream_text(self, agent, input, *, attempt_number):
                await self.report_request("answer_requested")
                while True:
                    chunk = await control.receive("answer")
                    yield chunk["text"]
                    if chunk["finished"]:
                        return

        responses = ControlledResponses()
        responses.search_hits = [
            ExternalSearchHit(
                url="https://example.com/initial-report",
                title="売上報告",
                content="売上は前年同期比10%増",
                source_name="Example",
            )
        ]

        @asynccontextmanager
        async def runtime_scope():
            yield responses

        @asynccontextmanager
        async def search_scope():
            yield ExternalSearchService(
                query_runtime=responses, search_gateway=responses
            )

        with ExitStack() as patches:
            patches.enter_context(
                patch.object(broker_agent, "_ack_generator", observe_ack)
            )
            for name in (
                "activate_gemini_agent_runtime",
                "activate_evidence_reviewer_runtime",
            ):
                patches.enter_context(patch.object(composition, name, runtime_scope))
            patches.enter_context(
                patch.object(composition, "activate_external_search", search_scope)
            )
            patches.enter_context(
                patch.object(gemini, "GeminiQueryEmbedder", lambda **_: responses)
            )
            patches.enter_context(
                patch.object(broker_agent, "listen", receive_deliveries)
            )
            patches.enter_context(
                patch.object(
                    AgentRunCompletionRepository, "complete_run", observe_completion
                )
            )
            patches.enter_context(
                patch.object(
                    AgentAnswerGenerationRepository,
                    "start_answer_generation",
                    observe_answer_start,
                )
            )
            broker_agent.is_worker_process = True
            broker_agent.add_middlewares(DeliveryObserver())
            try:
                await broker_agent.startup()
                receiver = Receiver(
                    broker_agent,
                    max_async_tasks=1,
                    max_prefetch=1,
                    run_startup=False,
                    ack_type=AcknowledgeType.WHEN_EXECUTED,
                    wait_tasks_timeout=2,
                )
                async with asyncio.TaskGroup() as tasks:
                    receiving = tasks.create_task(receiver.listen(finish))
                    stopping = tasks.create_task(
                        redis.blpop(f"agent-test:{control.name}:stop", timeout=0)
                    )
                    await control.send("events", {"kind": "ready"})
                    done, _ = await asyncio.wait(
                        (receiving, stopping), return_when=asyncio.FIRST_COMPLETED
                    )
                    finish.set()
                    if receiving in done:
                        stopping.cancel()
                        raise RuntimeError(
                            "停止要求より前にワーカーの受信が終了しました"
                        )
            finally:
                await broker_agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
