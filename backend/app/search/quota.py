"""Search エンドポイント向け per-user 日次クォータ (Redis アトミック消費)。

red-team C1 chain (anon DoS + Gemini 課金枯渇) を構造的に塞ぐためのレイヤ:
  1. embedding cache miss を強制する q=$RANDOM 攻撃を per-user で daily_max にキャップ
  2. Better Auth pg.Pool 飽和の上流で fail-fast (embedder 呼出前に 429)

設計判断:
  - per-user (UUID) 単位。anon は router 側 auth dependency で先に弾く
  - Lua atomic で GET → 比較 → INCRBY → EXPIRE を一発実行 (multi-worker 競合下でも厳守)
  - fail-close: Redis 不通 → redis.eval が例外 → caller (service) で伝播 (default 500)
  - admin も同 quota を消費 (admin が課金抜け道になる構造リスクを排除)
"""

from __future__ import annotations

from uuid import UUID

import redis.asyncio as aioredis

from app.maintenance.policy import utc_now

_QUOTA_TTL_SECONDS = 26 * 60 * 60  # 26h: 日跨ぎ猶予 (budget.py 同ポリシ)

# Lua は backfill budget からコピー。意図的に共通化せず責務を分離する
# (memory feedback_no_share_different_problems.md: 解いている問題が違うなら共用しない)。
_LUA_CONSUME_QUOTA = """
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


class SearchQuotaExceededError(Exception):
    """ユーザーが当日のセマンティック検索クォータを使い切った。"""


def _quota_key(user_id: UUID) -> str:
    """``search:quota:user:{user_id}:{YYYYMMDD}`` (UTC) を返す。"""
    return f"search:quota:user:{user_id}:{utc_now().strftime('%Y%m%d')}"


async def consume_search_quota(
    redis: aioredis.Redis,
    user_id: UUID,
    requested: int,
    daily_max: int,
) -> int:
    """当日の per-user 検索クォータを atomic に消費し、許可された件数を返す。

    Args:
        redis: 共有 Redis クライアント。
        user_id: BFF JWT の sub から取り出した UUID。
        requested: 消費したい件数 (通常 1: 1 query = 1 embedding 生成)。
        daily_max: 当日のユーザー上限。0 以下を渡すと configuration error。

    Returns:
        実際に消費 (= 検索を許可) された件数。

    Raises:
        ValueError: daily_max が 0 以下。
        SearchQuotaExceededError: 既に上限到達 (granted == 0 のとき)。
        redis.RedisError: Redis 不通時 (fail-close、伝播)。
    """
    if daily_max <= 0:
        raise ValueError(f"daily_max must be positive: {daily_max}")
    if requested <= 0:
        return 0
    granted = await redis.eval(
        _LUA_CONSUME_QUOTA,
        1,
        _quota_key(user_id),
        requested,
        daily_max,
        _QUOTA_TTL_SECONDS,
    )
    granted_int = int(granted)
    if granted_int == 0:
        raise SearchQuotaExceededError(
            f"Daily search quota exhausted (max={daily_max})"
        )
    return granted_int
