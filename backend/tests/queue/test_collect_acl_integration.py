"""production collect ACLを一時userへ適用する実Redis DRYRUN契約。"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from redis import asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import settings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.xdist_group("redis"),
]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_REDIS_FLY_CONFIG = _REPOSITORY_ROOT / "infra" / "redis" / "fly.toml"


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


@pytest.fixture
async def temporary_collect_acl_user() -> AsyncIterator[tuple[Redis, str]]:
    """final collect rulesだけを一意なnopass userへ適用し、必ず削除する。"""
    redis = aioredis.from_url(settings.redis_url)
    username = f"test_collect_{uuid4().hex}"
    try:
        await redis.execute_command(
            "ACL",
            "SETUSER",
            username,
            "reset",
            *_collect_acl_rules(),
            "nopass",
        )
        yield redis, username
    finally:
        await redis.acl_deluser(username)
        await redis.aclose()


async def _is_denied(redis: Redis, username: str, *command: str) -> bool:
    try:
        response = await redis.acl_dryrun(username, *command)
    except ResponseError:
        return True
    if isinstance(response, bytes):
        response = response.decode()
    return response.casefold() != "ok"


async def test_collect_acl_allows_required_surfaces_and_denies_core_streams(
    temporary_collect_acl_user: tuple[Redis, str],
) -> None:
    """DRYRUNだけでproducer/consumer/result key境界を検証し、entryは書かない。"""
    redis, username = temporary_collect_acl_user
    allowed_commands = (
        ("XADD", "pipeline:curation", "*", "data", "taskiq-payload"),
        (
            "XREADGROUP",
            "GROUP",
            "taskiq",
            "temporary-consumer",
            "COUNT",
            "1",
            "STREAMS",
            "pipeline:metadata",
            ">",
        ),
        ("XACK", "pipeline:metadata", "taskiq", "0-0"),
        ("XADD", "pipeline:content", "*", "data", "taskiq-payload"),
        ("SET", "taskiq:temporary-result", "payload", "EX", "60"),
        ("GET", "taskiq:temporary-result"),
        ("EXISTS", "taskiq:temporary-result"),
    )
    denied_streams = (
        "pipeline:analysis",
        "pipeline:assessment",
        "pipeline:embedding",
        "pipeline:maintenance",
    )

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

    assert allowed == [b"OK"] * len(allowed_commands) and denied == {
        stream: True for stream in denied_streams
    }
