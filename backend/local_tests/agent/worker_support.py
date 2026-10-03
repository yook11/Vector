"""専用Redisと別プロセスのワーカーを起動し、配送と回答生成を同期する。"""

import asyncio
import json
import signal
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent.answering.timing import answer_generation_recovery_window
from app.agent.live_updates.stream import agent_run_live_stream_key
from app.db.session import caller_managed_session_factory
from app.queue.messages.agent_run import AgentRunTrigger
from app.queue.tasks.agent_run import run_agent_answer
from app.redis.clients import taskiq_stream_connection
from app.redis.taskiq_stream_broker import create_taskiq_stream_broker
from local_tests.agent.support import fetch_saved_run_result
from local_tests.database import ROOT, RunnerSettings, _run

WAIT_SECONDS = 30


@dataclass(frozen=True)
class PendingDelivery:
    stream_message_id: str
    task_id: str
    run_id: str
    consumer_name: str


@dataclass(frozen=True)
class ProcessedDelivery:
    stream_message_id: str
    task_id: str
    worker_pid: int
    answer_generation_calls: int
    acknowledged: bool


@dataclass
class WorkerControl:
    redis: Redis
    name: str

    async def send(self, channel, payload):
        await self.redis.rpush(f"agent-test:{self.name}:{channel}", json.dumps(payload))

    async def receive(self, channel, *, timeout=WAIT_SECONDS):
        async with asyncio.timeout(timeout):
            item = await self.redis.blpop(
                f"agent-test:{self.name}:{channel}", timeout=0
            )
        return json.loads(item[1])

    async def expect(self, kind):
        event = await self.receive("events")
        if (
            kind in {"planning_requested", "answer_requested"}
            and event["kind"] == "delivery_completed"
        ):
            raise RuntimeError(
                f"worker {self.name}: {kind}に到達せず配送が終了しました: {event}"
            )
        if event["kind"] != kind:
            raise RuntimeError(f"worker {self.name}: expected {kind}, got {event}")
        return event


@dataclass
class WorkerExecution:
    control: WorkerControl
    worker_pid: int
    attempt_epoch: int
    task_id: str
    run_id: str
    consumer_name: str
    process: asyncio.subprocess.Process
    answer_started_at: datetime | None = None

    async def resume_planning(self):
        await self.control.send("planning", {})

    async def wait_for_answer_request(self):
        event = await self.control.expect("answer_requested")
        if (
            event["task_id"] != self.task_id
            or event["run_id"] != self.run_id
            or event["attempt_epoch"] != self.attempt_epoch
        ):
            raise RuntimeError("Plannerと回答生成の対象世代が一致していません")
        self.answer_started_at = datetime.fromisoformat(event["answer_started_at"])
        return self

    async def respond(self, answer):
        await self.control.send("answer", {"text": answer, "finished": True})

    async def wait_until_before_ack(self):
        event = await self.control.expect("before_ack")
        if event["task_id"] != self.task_id:
            raise RuntimeError("ACK直前で停止した配送が一致しません")
        message_id = event["stream_message_id"]
        pending = await self.control.redis.xpending_range(
            "agent", "taskiq", message_id, message_id, 1
        )
        if len(pending) != 1 or pending[0]["consumer"] != self.consumer_name:
            raise RuntimeError("担当ワーカーの未ACK配送を確認できません")
        return PendingDelivery(
            message_id, self.task_id, self.run_id, self.consumer_name
        )

    async def stream_partial_answer(self, text):
        await self.control.send("answer", {"text": text, "finished": False})
        stream = agent_run_live_stream_key(self.run_id)
        cursor = "0-0"
        received = ""
        async with asyncio.timeout(5):
            while received != text:
                streams = await self.control.redis.xread({stream: cursor}, block=1000)
                for _, entries in streams:
                    for cursor, fields in entries:
                        if (
                            fields["type"] == "answer.delta"
                            and int(fields["attemptEpoch"]) == self.attempt_epoch
                        ):
                            received += json.loads(fields["payload"])["text"]
                if not text.startswith(received):
                    raise RuntimeError("指定した回答断片と実Redisの出力が一致しません")

    @property
    def answer_recovery_deadline(self):
        if self.answer_started_at is None:
            raise RuntimeError("回答生成はまだ開始していません")
        return self.answer_started_at + answer_generation_recovery_window()

    async def kill(self):
        if self.process.pid != self.worker_pid or self.process.returncode is not None:
            raise RuntimeError("担当ワーカーが強制終了前に停止しています")
        self.process.kill()
        returncode = await asyncio.wait_for(self.process.wait(), timeout=5)
        if returncode != -signal.SIGKILL:
            raise RuntimeError("担当ワーカーをSIGKILLで終了できませんでした")
        pending = await self.control.redis.xpending_range(
            "agent", "taskiq", "-", "+", 1, consumername=self.consumer_name
        )
        if not pending:
            raise RuntimeError("強制終了した配送が未ACKとして残っていません")

    async def wait_for_delivery_completion(self):
        """保存の判定と配送のACKが終わり、DB・通知を観測できるまで待つ。"""
        event = await self._wait_for_processed_delivery()
        if not event["completion_returned"]:
            raise RuntimeError(f"回答の保存判定まで完了していません: {event}")

    async def wait_for_answer_generation_stop(self):
        event = await self._wait_for_processed_delivery()
        return {
            "stop_reason": event["answer_generation_stop_reason"],
            "completion_returned": event["completion_returned"],
        }

    async def _wait_for_processed_delivery(self):
        event = await self.control.expect("delivery_completed")
        if event["task_id"] != self.task_id or event["is_err"]:
            raise RuntimeError(f"配送処理が正常終了していません: {event}")
        pending = await self.control.redis.xpending_range(
            "agent", "taskiq", "-", "+", 1, consumername=self.consumer_name
        )
        if pending:
            raise RuntimeError("処理後も配送がRedisの未ACK一覧に残っています")
        return event


class AgentWorkers:
    def __init__(
        self,
        database,
        owner_session_factory,
        broker,
        controls,
        processes,
        settings,
        start_process,
    ):
        self.database = database
        self.owner_session_factory = owner_session_factory
        self.broker = broker
        self.controls = controls
        self.processes = processes
        self.settings = settings
        self.start_process = start_process
        self.deliveries = []
        self.next_request = 0

    async def enqueue(self, run_id, *, pause_before_ack=False):
        control = self.controls[len(self.deliveries)]
        await control.send(
            "receive", {"continuous": False, "pause_before_ack": pause_before_ack}
        )
        task = (
            await run_agent_answer.kicker()
            .with_broker(self.broker)
            .kiq(trigger=AgentRunTrigger(run_id=run_id))
        )
        self.deliveries.append((control, task.task_id, str(run_id)))

    async def wait_for_planning_request(self):
        control, task_id, run_id = self.deliveries[self.next_request]
        event = await control.expect("planning_requested")
        if event["task_id"] != task_id or event["run_id"] != run_id:
            raise RuntimeError("要求した配送とPlannerの対象が一致していません")
        self.next_request += 1
        return WorkerExecution(
            control=control,
            worker_pid=event["worker_pid"],
            attempt_epoch=event["attempt_epoch"],
            task_id=task_id,
            run_id=run_id,
            consumer_name=event["consumer_name"],
            process=self.processes[control.name],
        )

    async def wait_for_answer_request(self):
        execution = await self.wait_for_planning_request()
        await execution.resume_planning()
        return await execution.wait_for_answer_request()

    async def wait_for_skipped_delivery(self):
        control, task_id, run_id = self.deliveries[self.next_request]
        event = await control.expect("delivery_completed")
        if (
            event["task_id"] != task_id
            or event["run_id"] != run_id
            or event["worker_pid"] != self.processes[control.name].pid
            or event["is_err"]
            or event["completion_returned"]
        ):
            raise RuntimeError(f"重複配送が再実行なしで終了していません: {event}")
        self.next_request += 1
        message_id = event["stream_message_id"]
        pending = await control.redis.xpending_range(
            "agent", "taskiq", message_id, message_id, 1
        )
        return ProcessedDelivery(
            stream_message_id=message_id,
            task_id=event["task_id"],
            worker_pid=event["worker_pid"],
            answer_generation_calls=event["answer_generation_calls"],
            acknowledged=event["acknowledged"] and not pending,
        )

    async def start_unacknowledged_recovery(self):
        control = self.controls[len(self.deliveries)]
        await control.send("receive", {"continuous": True, "recover_pending": True})
        event = await control.expect("recovery_started")
        if event["worker_pid"] != self.processes[control.name].pid:
            raise RuntimeError("未ACK回収を開始したワーカーが一致しません")

    async def count_stream_entries(self):
        return await self.controls[0].redis.xlen("agent")

    async def recover_unacknowledged_delivery(self, pending_delivery):
        control = self.controls[len(self.deliveries)]
        message_id = pending_delivery.stream_message_id
        pending = await control.redis.xpending_range(
            "agent", "taskiq", message_id, message_id, 1
        )
        if (
            len(pending) != 1
            or pending[0]["consumer"] != pending_delivery.consumer_name
        ):
            raise RuntimeError("死亡したワーカーの未ACK配送が残っていません")
        await self.start_unacknowledged_recovery()
        return await self.wait_for_recovered_delivery(pending_delivery)

    async def wait_for_recovered_delivery(self, pending_delivery):
        control = self.controls[len(self.deliveries)]
        message_id = pending_delivery.stream_message_id
        try:
            event = await control.expect("delivery_completed")
        except TimeoutError as exc:
            pending = await control.redis.xpending_range(
                "agent", "taskiq", message_id, message_id, 1
            )
            stream_entries = await self.count_stream_entries()
            worker_alive = self.processes[control.name].returncode is None
            raise AssertionError(
                f"{WAIT_SECONDS}秒以内に未ACK配送が回収されませんでした: "
                f"worker_alive={worker_alive}, stream_entries={stream_entries}, "
                f"pending={pending}"
            ) from exc
        if (
            event["task_id"] != pending_delivery.task_id
            or event["run_id"] != pending_delivery.run_id
            or event["stream_message_id"] != message_id
            or event["worker_pid"] != self.processes[control.name].pid
            or event["consumer_name"] == pending_delivery.consumer_name
            or event["is_err"]
        ):
            raise RuntimeError("別ワーカーによる同じ配送の回収を確認できません")
        pending = await control.redis.xpending_range(
            "agent", "taskiq", message_id, message_id, 1
        )
        return ProcessedDelivery(
            stream_message_id=event["stream_message_id"],
            task_id=event["task_id"],
            worker_pid=event["worker_pid"],
            answer_generation_calls=event["answer_generation_calls"],
            acknowledged=event["acknowledged"] and not pending,
        )

    async def observe_run(self, *, run_id, thread_id):
        saved = await fetch_saved_run_result(
            self.database, run_id=run_id, thread_id=thread_id
        )
        entries = await self.controls[0].redis.xrange(agent_run_live_stream_key(run_id))
        return {
            "status": saved.run["status"],
            "attempt_epoch": saved.run["attempt_epoch"],
            "answers": [message["content"] for message in saved.assistant_messages],
            "sources": [
                {"source_ref": source["source_ref"], "url": source["url"]}
                for source in saved.sources
            ],
            "terminal_events": [
                {
                    "attempt_epoch": int(fields["attemptEpoch"]),
                    "status": json.loads(fields["payload"])["status"],
                }
                for _, fields in entries
                if fields["type"] == "terminal"
            ],
        }


@asynccontextmanager
async def isolated_redis():
    env = {"PATH": RunnerSettings().process_path}
    compose = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        f"vector-agent-workers-{uuid4().hex[:12]}",
        "-f",
        str(ROOT / "docker-compose.test.yml"),
    ]
    try:
        await asyncio.to_thread(
            _run, [*compose, "up", "-d", "--wait", "redis-test"], cwd=ROOT, env=env
        )
        address = await asyncio.to_thread(
            _run, [*compose, "port", "redis-test", "6379"], cwd=ROOT, env=env
        )
        host, port = address.strip().rsplit(":", 1)
        if host != "127.0.0.1":
            raise RuntimeError("ワーカーのテスト用Redisはloopback接続が必要です")
        yield f"redis://127.0.0.1:{int(port)}/0"
    finally:
        await asyncio.to_thread(
            _run, [*compose, "down", "-v", "--remove-orphans"], cwd=ROOT, env=env
        )


async def _stop_worker(process, control):
    if process.returncode is not None:
        return
    try:
        await control.send("stop", {})
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


@asynccontextmanager
async def start_agent_workers(database, log_directory):
    async with AsyncExitStack() as stack:
        redis_url = await stack.enter_async_context(isolated_redis())
        redis = Redis.from_url(redis_url, decode_responses=True, socket_timeout=None)
        stack.push_async_callback(redis.aclose)
        settings = SimpleNamespace(
            database_url=database.url("vector_agent", sqlalchemy=True),
            db_iam_auth=False,
            redis_url=redis_url,
            redis_iam_auth=False,
            redis_iam_cache_name=None,
            aws_region=None,
        )
        # runの作成はAPIの受付に当たるため、ワーカーのロールと分けて所有者で行う。
        owner_engine = create_async_engine(database.url("vector", sqlalchemy=True))
        stack.push_async_callback(owner_engine.dispose)
        broker = create_taskiq_stream_broker(
            taskiq_stream_connection(settings), "agent"
        )
        stack.push_async_callback(broker.shutdown)
        await broker.startup()
        env = {
            "PATH": RunnerSettings().process_path,
            "DATABASE_URL": settings.database_url,
            "REDIS_URL": redis_url,
            "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
            "AWS_REGION": "ap-northeast-1",
            "BFF_JWT_SIGNING_SECRET": uuid4().hex,
            "REVALIDATE_BEARER_SECRET": uuid4().hex,
            "CROSSREF_CONTACT_EMAIL": "crossref-contact@example.invalid",
            "FRONTEND_URL": "http://localhost:3000",
            "INTERNAL_FRONTEND_BASE_URL": "http://localhost:3000",
            "GEMINI_API_KEY": "local-test",
            "AGENTCORE_GATEWAY_URL": "https://test.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com",
        }
        processes = {}

        async def start_process(name, module):
            control = WorkerControl(redis, name)
            log_path = log_directory / f"{control.name}.log"
            output = stack.enter_context(log_path.open("wb"))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                module,
                control.name,
                cwd=ROOT / "backend",
                env=env,
                stdout=output,
                stderr=output,
            )
            stack.push_async_callback(_stop_worker, process, control)
            processes[name] = process
            try:
                await control.expect("ready")
            except (TimeoutError, RuntimeError) as exc:
                raise RuntimeError(f"ワーカー起動失敗: {log_path}") from exc
            return control

        controls = [
            await start_process(name, "local_tests.agent.worker_process")
            for name in ("older", "latest")
        ]
        yield AgentWorkers(
            database,
            caller_managed_session_factory(owner_engine),
            broker,
            controls,
            processes,
            settings,
            start_process,
        )
