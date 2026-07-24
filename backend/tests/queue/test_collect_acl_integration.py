"""production collect ACLの実credential smoke / DRYRUN契約。"""

from __future__ import annotations

import asyncio
import re
import shlex
import tomllib
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from redis import asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from taskiq import TaskiqResult
from taskiq.message import TaskiqMessage
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.config import settings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_REDIS_FLY_CONFIG = _REPOSITORY_ROOT / "infra" / "redis" / "fly.toml"
_DISPATCH_STREAM = "pipeline:dispatch"
_ACQUISITION_STREAM = "pipeline:acquisition"
_COMPLETION_STREAM = "pipeline:completion"
_GROUP = "taskiq"


@dataclass(frozen=True)
class TemporaryCollectUser:
    """一時collect userと、そのuserでAUTHする接続先。"""

    admin: Redis
    username: str
    redis_url: str


def _collect_acl_rules() -> list[str]:
    config = tomllib.loads(_REDIS_FLY_CONFIG.read_text(encoding="utf-8"))
    redis_command = config["processes"]["redis"]
    match = re.search(r'echo "user collect (?P<rules>[^\"]+)"', redis_command)
    assert match is not None, "collect ACL is missing from infra/redis/fly.toml"
    return [
        token
        for token in shlex.split(match.group("rules"))
        if not token.startswith(">")
    ]


def _with_credentials(url: str, *, username: str, password: str) -> str:
    """既存Redis URLの接続先を保ち、認証情報だけを一時userへ差し替える。"""
    parsed = urlsplit(url)
    assert parsed.scheme in {"redis", "rediss"}
    assert parsed.hostname is not None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port is not None else ""
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}{port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in {"username", "password"}
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


@pytest.fixture
async def temporary_collect_acl_user() -> AsyncIterator[TemporaryCollectUser]:
    """final collect rulesをpassword付き一時userへ適用し、必ず削除する。"""
    redis_url = str(settings.redis_url)
    redis = aioredis.from_url(redis_url)
    username = f"test_collect_{uuid4().hex}"
    password = f"test-{uuid4().hex}-{uuid4().hex}"
    try:
        await redis.execute_command(
            "ACL",
            "SETUSER",
            username,
            "reset",
            *_collect_acl_rules(),
            f">{password}",
        )
        yield TemporaryCollectUser(
            admin=redis,
            username=username,
            redis_url=_with_credentials(
                redis_url,
                username=username,
                password=password,
            ),
        )
    finally:
        try:
            await redis.acl_deluser(username)
        finally:
            await redis.aclose()


async def _is_denied(redis: Redis, username: str, *command: str) -> bool:
    try:
        response = await redis.acl_dryrun(username, *command)
    except ResponseError:
        return True
    if isinstance(response, bytes):
        response = response.decode()
    return response.casefold() != "ok"


async def test_collect_acl_allows_required_and_denies_out_of_scope_surfaces(
    temporary_collect_acl_user: TemporaryCollectUser,
) -> None:
    """DRYRUNだけでproducer/consumer/result key境界を検証し、entryは書かない。"""
    redis = temporary_collect_acl_user.admin
    username = temporary_collect_acl_user.username
    allowed_commands = (
        ("MULTI",),
        ("EXEC",),
        ("XADD", "pipeline:acquisition", "*", "data", "taskiq-payload"),
        (
            "XREADGROUP",
            "GROUP",
            "taskiq",
            "temporary-consumer",
            "COUNT",
            "1",
            "STREAMS",
            "pipeline:acquisition",
            ">",
        ),
        ("XACK", "pipeline:acquisition", "taskiq", "0-0"),
        (
            "XAUTOCLAIM",
            "pipeline:acquisition",
            "taskiq",
            "temporary-consumer",
            "60000",
            "0-0",
            "COUNT",
            "1",
        ),
        ("XADD", "pipeline:completion", "*", "data", "taskiq-payload"),
        (
            "XREADGROUP",
            "GROUP",
            "taskiq",
            "temporary-consumer",
            "COUNT",
            "1",
            "STREAMS",
            "pipeline:completion",
            ">",
        ),
        ("XACK", "pipeline:completion", "taskiq", "0-0"),
        (
            "XAUTOCLAIM",
            "pipeline:completion",
            "taskiq",
            "temporary-consumer",
            "60000",
            "0-0",
            "COUNT",
            "1",
        ),
        ("XADD", _DISPATCH_STREAM, "*", "data", "taskiq-payload"),
        (
            "XREADGROUP",
            "GROUP",
            "taskiq",
            "temporary-consumer",
            "COUNT",
            "1",
            "STREAMS",
            _DISPATCH_STREAM,
            ">",
        ),
        ("XACK", _DISPATCH_STREAM, "taskiq", "0-0"),
        ("XADD", "pipeline:curation", "*", "data", "taskiq-payload"),
        (
            "SET",
            f"autoclaim:taskiq:{_DISPATCH_STREAM}",
            "temporary-consumer",
            "PX",
            "60000",
            "NX",
        ),
        (
            "SET",
            "autoclaim:taskiq:pipeline:acquisition",
            "temporary-consumer",
            "PX",
            "60000",
            "NX",
        ),
        (
            "SET",
            "autoclaim:taskiq:pipeline:completion",
            "temporary-consumer",
            "PX",
            "60000",
            "NX",
        ),
        ("SET", "taskiq:temporary-result", "payload", "EX", "60"),
        ("GET", "taskiq:temporary-result"),
        ("EXISTS", "taskiq:temporary-result"),
    )
    denied_streams = (
        "pipeline:metadata",
        "pipeline:content",
        "pipeline:analysis",
        "pipeline:assessment",
        "pipeline:embedding",
        "pipeline:maintenance",
    )
    denied_locks = tuple(f"autoclaim:taskiq:{stream}" for stream in denied_streams)

    allowed = [
        await redis.acl_dryrun(username, *command) for command in allowed_commands
    ]
    denied = {
        stream: await _is_denied(
            redis,
            username,
            "XADD",
            stream,
            "*",
            "data",
            "taskiq-payload",
        )
        for stream in denied_streams
    }
    denied_lock_writes = {
        lock: await _is_denied(redis, username, "SET", lock, "owner")
        for lock in denied_locks
    }

    assert (
        allowed,
        denied,
        denied_lock_writes,
    ) == (
        [b"OK"] * len(allowed_commands),
        {stream: True for stream in denied_streams},
        {lock: True for lock in denied_locks},
    )


async def test_collect_credentials_run_broker_autoclaim_recovery_and_result_smoke(
    temporary_collect_acl_user: TemporaryCollectUser,
) -> None:
    """collect AUTHで通常配達後のauto-claim回収/ACKと補助keyを実操作する。"""
    case_id = uuid4().hex
    result_id = f"acl-smoke-{case_id}"
    result_key = f"taskiq:{result_id}"
    locks = (
        f"autoclaim:{_GROUP}:{_ACQUISITION_STREAM}",
        f"autoclaim:{_GROUP}:{_COMPLETION_STREAM}",
    )
    cleanup_keys = (
        _ACQUISITION_STREAM,
        _COMPLETION_STREAM,
        *locks,
        result_key,
    )
    admin = temporary_collect_acl_user.admin
    await admin.delete(*cleanup_keys)

    result_backend = RedisAsyncResultBackend(
        redis_url=temporary_collect_acl_user.redis_url,
        result_ex_time=60,
        prefix_str="taskiq",
    )
    broker = RedisStreamBroker(
        url=temporary_collect_acl_user.redis_url,
        queue_name=_ACQUISITION_STREAM,
        additional_streams={_COMPLETION_STREAM: ">"},
        consumer_group_name=_GROUP,
        consumer_id="0-0",
        maxlen=10_000,
        xread_block=20,
        idle_timeout=50,
        unacknowledged_batch_size=100,
        unacknowledged_lock_timeout=60,
    ).with_result_backend(result_backend)
    collect = aioredis.from_url(temporary_collect_acl_user.redis_url)

    try:
        assert await collect.ping()
        await broker.startup()

        expected_stale_ids: set[str] = set()
        for stream in (_ACQUISITION_STREAM, _COMPLETION_STREAM):
            stale = TaskiqMessage(
                task_id=f"{case_id}-stale-{stream.rsplit(':', 1)[-1]}",
                task_name="collect_acl_smoke",
                labels={"queue_name": stream},
                args=[],
                kwargs={},
            )
            expected_stale_ids.add(stale.task_id)
            await broker.kick(broker.formatter.dumps(stale))
            delivered = await collect.xreadgroup(
                _GROUP,
                f"stale-{case_id}",
                {stream: ">"},
                count=1,
            )
            stale_message_id = delivered[0][1][0][0]
            await collect.xclaim(
                stream,
                _GROUP,
                f"stale-{case_id}",
                min_idle_time=0,
                message_ids=[stale_message_id],
                idle=1_000,
            )

        expected_normal_ids: set[str] = set()
        for stream in (_ACQUISITION_STREAM, _COMPLETION_STREAM):
            message = TaskiqMessage(
                task_id=f"{case_id}-{stream.rsplit(':', 1)[-1]}",
                task_name="collect_acl_smoke",
                labels={"queue_name": stream},
                args=[],
                kwargs={},
            )
            expected_normal_ids.add(message.task_id)
            await broker.kick(broker.formatter.dumps(message))

        received_normal_ids: set[str] = set()
        recovered_stale_ids: set[str] = set()
        async with aclosing(broker.listen()) as listener:
            for _ in range(2):
                delivery = await asyncio.wait_for(anext(listener), timeout=2)
                received = broker.formatter.loads(delivery.data)
                received_normal_ids.add(received.task_id)
                await delivery.ack()
            for _ in range(2):
                delivery = await asyncio.wait_for(anext(listener), timeout=2)
                recovered = broker.formatter.loads(delivery.data)
                recovered_stale_ids.add(recovered.task_id)
                await delivery.ack()

        for lock_key in locks:
            lock = collect.lock(lock_key, timeout=60)
            assert await lock.acquire(blocking=False)
            await lock.release()

        expected_result = TaskiqResult(
            is_err=False,
            return_value={"credential": "collect"},
            execution_time=0,
        )
        await result_backend.set_result(result_id, expected_result)
        stored_result = await result_backend.get_result(result_id)

        assert (
            received_normal_ids,
            recovered_stale_ids,
            int((await collect.xpending(_ACQUISITION_STREAM, _GROUP))["pending"]),
            int((await collect.xpending(_COMPLETION_STREAM, _GROUP))["pending"]),
            await result_backend.is_result_ready(result_id),
            stored_result.return_value,
        ) == (
            expected_normal_ids,
            expected_stale_ids,
            0,
            0,
            True,
            {"credential": "collect"},
        )
    finally:
        try:
            await broker.shutdown()
        finally:
            try:
                await collect.aclose()
            finally:
                await admin.delete(*cleanup_keys)
