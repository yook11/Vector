/**
 * Application-level rate limit (Redis-backed sliding window log)。
 *
 * Better Auth 内蔵の rate limit は HTTP router (/api/auth/*) にしか効かず、
 * Vector が依拠する `auth.api.getSession({ headers })` 直呼び経路には
 * 完全にバイパスされる (red-team C8 / F17)。本モジュールが proxy.ts で
 * 呼ばれることで、認証済 cookie を保持した攻撃者による DB Pool 飽和 DoS を
 * 構造的に bound する。
 *
 * フェイルオープン: Redis 不通時は throttle を skip して通す。Redis 障害が
 * 全リクエスト 503 に直結しないようにし、一次防衛線は pg.Pool 設定に委ねる。
 */

import "server-only";
import { createClient, type RedisClientType } from "redis";

import type { RequestIdentifier } from "@/lib/proxy/identifier";

const WINDOW_SEC = 60;
const WINDOW_MS = WINDOW_SEC * 1000;
const DEFAULT_AUTHED_LIMIT = 120;
const DEFAULT_ANON_LIMIT = 60;

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
  kind: RequestIdentifier["kind"],
  env: Record<string, string | undefined> = process.env,
): number {
  if (kind === "auth") {
    return parseLimit(env.RATE_LIMIT_AUTHED_PER_MIN, DEFAULT_AUTHED_LIMIT);
  }
  return parseLimit(env.RATE_LIMIT_ANON_PER_MIN, DEFAULT_ANON_LIMIT);
}

// HMR / vitest module reset で client が複数生成されないよう globalThis にぶら下げる
const globalForRedis = globalThis as unknown as {
  __vectorRateLimitRedis?: RedisClientType;
  __vectorRateLimitErrorLogged?: boolean;
};

function logRedisError(context: string, err: unknown): void {
  if (globalForRedis.__vectorRateLimitErrorLogged) return;
  console.warn(`rate-limit: ${context}, failing open`, err);
  globalForRedis.__vectorRateLimitErrorLogged = true;
}

function getClient(): RedisClientType | null {
  if (globalForRedis.__vectorRateLimitRedis) {
    return globalForRedis.__vectorRateLimitRedis;
  }
  const url = process.env.REDIS_URL;
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
  const c = getClient();
  if (!c) {
    return { allowed: true };
  }
  try {
    if (!c.isOpen) {
      await c.connect();
    }
    const limit = calculateLimit(identifier.kind);
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
