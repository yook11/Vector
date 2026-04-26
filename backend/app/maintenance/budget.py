"""back-fill の日次予算 (Redis アトミック消費)。

役割ごとに 1 日あたりの最大投入数を制限し、暴走時の課金事故と DB 過負荷を
防ぐ。``daily_max`` はメインフローを枯渇させない安全マージンを残して設定する。

複数 worker 同時実行下でも厳密に上限を守るため Lua スクリプトで GET / 比較 /
INCRBY / EXPIRE を atomic に実行する。
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.maintenance.policy import utc_now

_BUDGET_TTL_SECONDS = 26 * 60 * 60  # 26h: 日跨ぎの猶予

_LUA_CONSUME_BUDGET = """
local key = KEYS[1]
local requested = tonumber(ARGV[1])
local daily_max = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local current = tonumber(redis.call('GET', key) or 0)
local available = daily_max - current
if available <= 0 then
    return 0
end
local granted = requested
if granted > available then
    granted = available
end
redis.call('INCRBY', key, granted)
redis.call('EXPIRE', key, ttl)
return granted
"""


def _budget_key(role: str) -> str:
    """``backfill:budget:{role}:{YYYYMMDD}`` (UTC) を返す。"""
    return f"backfill:budget:{role}:{utc_now().strftime('%Y%m%d')}"


async def consume_daily_budget(
    redis: aioredis.Redis,
    role: str,
    requested: int,
    daily_max: int,
) -> int:
    """当日の back-fill 予算を atomic に消費し、許可された件数を返す。

    Args:
        redis: 共有 Redis クライアント。
        role: ``extract`` / ``classify`` / ``embed`` のいずれか。
        requested: dispatch したい件数 (backlog SELECT の結果数)。
        daily_max: 当日の上限。0 以下を渡すと例外。

    Returns:
        実際に消費 (= dispatch を許可) された件数。0 なら本日は打ち切り。
    """
    if daily_max <= 0:
        raise ValueError(f"daily_max must be positive: {daily_max}")
    if requested <= 0:
        return 0
    granted = await redis.eval(
        _LUA_CONSUME_BUDGET,
        1,
        _budget_key(role),
        requested,
        daily_max,
        _BUDGET_TTL_SECONDS,
    )
    return int(granted)
