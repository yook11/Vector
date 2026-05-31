/**
 * Application-level rate limit (Redis-backed sliding window log)。
 *
 * Better Auth 内蔵 rate limit は /api/auth/* router 専用のため、
 * proxy.ts から全 request に適用し、認証済 cookie を持つ request も先に bound する。
 *
 * Redis 不通時は fail-open し、warn は 60 秒ごとに出す。identifier は IP のみに
 * 統一し、認証状態に応じた緩和は後段に任せる。
 *
 * 本モジュールの Redis client は proxy.ts の IP limiter 専用 (key prefix rl:ip:*)。
 * Better Auth のログイン limiter は DB-backed (storage:"database") に移行済みで
 * Redis を使わない (ADR-007)。
 */

import "server-only";
import { createClient, type RedisClientType } from "redis";

import type { RequestIdentifier } from "@/lib/proxy/identifier";

const WINDOW_SEC = 60;
const WINDOW_MS = WINDOW_SEC * 1000;
const DEFAULT_LIMIT = 60;
const ERROR_LOG_INTERVAL_MS = 60_000;

export type RateLimitDecision =
  | { allowed: true }
  | { allowed: false; retryAfterSeconds: number };

export function parseLimit(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return n;
}

export function calculateLimit(
  env: Record<string, string | undefined> = process.env,
): number {
  return parseLimit(env.RATE_LIMIT_PER_MIN, DEFAULT_LIMIT);
}

// HMR / vitest module reset で client が複数生成されないよう
// globalThis にぶら下げる。
const globalForRedis = globalThis as unknown as {
  __vectorRateLimitRedis?: RedisClientType;
  __vectorRateLimitErrorLastMs?: number;
};

function logRedisError(context: string, err: unknown): void {
  // Redis 永続障害を無音にしないため、warn は 60 秒ごとに継続出力する。
  const now = Date.now();
  const last = globalForRedis.__vectorRateLimitErrorLastMs;
  if (last !== undefined && now - last < ERROR_LOG_INTERVAL_MS) {
    return;
  }
  console.warn(`rate-limit: ${context}, failing open`, err);
  globalForRedis.__vectorRateLimitErrorLastMs = now;
}

/**
 * frontend 内の rate-limit 用 Redis client (singleton)。
 *
 * proxy.ts の sliding window log (`checkRateLimit`) 専用。
 * REDIS_URL_RL / REDIS_URL 未設定時は null を返し、呼び出し側が fail-open する。
 */
function getRateLimitRedisClient(): RedisClientType | null {
  if (globalForRedis.__vectorRateLimitRedis) {
    return globalForRedis.__vectorRateLimitRedis;
  }
  // rate-limit 専用 Redis があれば優先し、
  // 未設定なら既存 REDIS_URL に fallback する。
  // 空文字列は未設定と同じ扱いにする。
  const url = process.env.REDIS_URL_RL || process.env.REDIS_URL;
  if (!url) return null;
  const c = createClient({ url }) as RedisClientType;
  c.on("error", (err) => {
    logRedisError("redis client error", err);
  });
  globalForRedis.__vectorRateLimitRedis = c;
  return c;
}

// Sliding window log を ZSET で表現し、Lua script で 1 round trip atomic に判定する。
// KEYS[1]=key, ARGV[1]=nowMs, ARGV[2]=windowMs, ARGV[3]=limit, ARGV[4]=ttlSec
// returns 1 if allowed, 0 if denied
const SLIDING_WINDOW_SCRIPT = `
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = tonumber(redis.call('ZCARD', key))
if count >= limit then
  return 0
end
redis.call('ZADD', key, now, now .. ':' .. math.random())
redis.call('EXPIRE', key, ttl)
return 1
`.trim();

export async function checkRateLimit(
  identifier: RequestIdentifier,
): Promise<RateLimitDecision> {
  const c = getRateLimitRedisClient();
  if (!c) {
    return { allowed: true };
  }
  try {
    if (!c.isOpen) {
      await c.connect();
    }
    const limit = calculateLimit();
    const key = `rl:${identifier.kind}:${identifier.key}`;
    const result = (await c.eval(SLIDING_WINDOW_SCRIPT, {
      keys: [key],
      arguments: [
        String(Date.now()),
        String(WINDOW_MS),
        String(limit),
        String(WINDOW_SEC),
      ],
    })) as number;
    return result === 1
      ? { allowed: true }
      : { allowed: false, retryAfterSeconds: WINDOW_SEC };
  } catch (err) {
    logRedisError("eval failed", err);
    return { allowed: true };
  }
}
