/**
 * Application-level rate limit (Redis-backed sliding window log)。
 *
 * Better Auth 内蔵 rate limit は /api/auth/* router 専用のため、proxy.ts から全 request
 * に適用し、認証済 cookie を持つ request も先に bound する。
 *
 * 本モジュールは実行層 (server-only)。request を tier に分類する純関数は
 * `@/lib/proxy/rate-limit-plan` に分離してあり、ここでは plan を受けて Redis 上で
 * 「全 tier を満たせば allow / 1 つでも超過で block」を atomic に判定する。
 *
 * key namespace は plan が決める: `rl:rsc:* / rl:sess:* / rl:ip:* / rl:uwrite:global`。
 * Better Auth のログイン limiter は DB-backed (storage:"database") に移行済みで Redis を
 * 使わない (ADR-007 / ADR-009)。
 *
 * Redis 不通時・tiers 空時は fail-open し、warn は 60 秒ごとに出す。
 */

import "server-only";
import { randomUUID } from "node:crypto";
import { createClient, type RedisClientType } from "redis";

import {
  logServerEvent,
  type ServerLogEvent,
} from "@/lib/observability/server-log";
import type {
  RateLimitLimits,
  RateLimitPlan,
  RateLimitSignal,
} from "@/lib/proxy/rate-limit-plan";

const WINDOW_SEC = 60;
const WINDOW_MS = WINDOW_SEC * 1000;
const ERROR_LOG_INTERVAL_MS = 60_000;

// tier ごとのデフォルト上限 (req/min)。ADR-009。
const DEFAULT_RSC_LIMIT = 600;
const DEFAULT_SESSION_LIMIT = 60;
const DEFAULT_IP_LIMIT = 300;
const DEFAULT_UNKNOWN_WRITE_LIMIT = 30;

export type RateLimitDecision =
  | { allowed: true }
  | { allowed: false; retryAfterSeconds: number };

export function parseLimit(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return n;
}

/**
 * env から tier 別上限を組む。旧 `RATE_LIMIT_PER_MIN` は読まない (deprecate)。
 */
export function calculateLimits(
  env: Record<string, string | undefined> = process.env,
): RateLimitLimits {
  return {
    rsc: parseLimit(env.RATE_LIMIT_RSC_PER_MIN, DEFAULT_RSC_LIMIT),
    session: parseLimit(env.RATE_LIMIT_SESSION_PER_MIN, DEFAULT_SESSION_LIMIT),
    ip: parseLimit(env.RATE_LIMIT_IP_PER_MIN, DEFAULT_IP_LIMIT),
    unknownWrite: parseLimit(
      env.RATE_LIMIT_UNKNOWN_WRITE_PER_MIN,
      DEFAULT_UNKNOWN_WRITE_LIMIT,
    ),
  };
}

// HMR / vitest module reset で client が複数生成されないよう
// globalThis にぶら下げる。
const globalForRedis = globalThis as unknown as {
  __vectorRateLimitRedis?: RedisClientType;
  __vectorRateLimitErrorLastMs?: number;
  __vectorRateLimitSignalLastMs?: Record<string, number>;
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

const SIGNAL_EVENT: Record<RateLimitSignal, ServerLogEvent> = {
  missing_ip: "frontend_rate_limit_missing_ip",
  unknown_write: "frontend_rate_limit_unknown_write",
};

/**
 * 観測信号を記録する。logRedisError と同型の per-signal 60 秒 throttle で、
 * production の IP 未解決 (経路異常) をスパムにせず可視化する。
 */
export function recordRateLimitSignal(signal: RateLimitSignal): void {
  const now = Date.now();
  const store = globalForRedis.__vectorRateLimitSignalLastMs ?? {};
  globalForRedis.__vectorRateLimitSignalLastMs = store;
  const last = store[signal];
  if (last !== undefined && now - last < ERROR_LOG_INTERVAL_MS) {
    return;
  }
  store[signal] = now;
  logServerEvent("warn", SIGNAL_EVENT[signal]);
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

// 複数 tier の sliding window log を 1 round trip で atomic に判定する。
// 不変条件: 全 tier を先に ZCARD し、1 つでも上限以上なら deny して
// どの key にも ZADD しない / 全通過時のみ全 key に ZADD + EXPIRE。
//
// KEYS[1..N]     = tier key (plan.tiers 順)
// ARGV[1]        = nowMs
// ARGV[2]        = windowMs
// ARGV[3]        = ttlSec
// ARGV[4..3+N]   = 各 tier limit (KEYS 同順)
// ARGV[4+N]      = uniqueId (member 重複回避用、JS 側で生成)
// returns 1 if allowed, 0 if denied
const MULTI_SLIDING_WINDOW_SCRIPT = `
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local n = #KEYS
local member = now .. ':' .. ARGV[4 + n]

-- phase 1: prune + ZCARD + 超過判定 (どれか1つでも超えたら ZADD せず deny)
for i = 1, n do
  local key = KEYS[i]
  local limit = tonumber(ARGV[3 + i])
  redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
  local count = tonumber(redis.call('ZCARD', key))
  if count >= limit then
    return 0
  end
end

-- phase 2: 全 key に同一 member を ZADD + EXPIRE
for i = 1, n do
  local key = KEYS[i]
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, ttl)
end
return 1
`.trim();

/**
 * plan の全 tier を満たすか Redis 上で atomic に判定する。
 *
 * tiers 空 (構造的 fail-open) / client なし / eval throw はいずれも allow に倒す。
 */
export async function checkRateLimit(
  plan: RateLimitPlan,
): Promise<RateLimitDecision> {
  if (plan.tiers.length === 0) {
    return { allowed: true };
  }
  const c = getRateLimitRedisClient();
  if (!c) {
    return { allowed: true };
  }
  try {
    if (!c.isOpen) {
      await c.connect();
    }
    const result = (await c.eval(MULTI_SLIDING_WINDOW_SCRIPT, {
      keys: plan.tiers.map((t) => t.key),
      arguments: [
        String(Date.now()),
        String(WINDOW_MS),
        String(WINDOW_SEC),
        ...plan.tiers.map((t) => String(t.limit)),
        randomUUID(),
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
