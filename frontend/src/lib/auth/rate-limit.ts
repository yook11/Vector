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
 * 全リクエスト 503 に直結しないようにし、一次防衛線は pg.Pool 設定に委ねる
 * (ADR-006 §3)。永続障害が無音にならないよう、warn ログは 60 秒ごとに 1 度出す
 * (red-team C1 / F23 対策。一度きりログだと Redis ダウンを運用が見落とす)。
 *
 * limit は per-IP の 1 種類 (red-team C1 / F2-F4 対策で identifier を IP に統一)。
 * 認証状態に応じた limit 緩和は本モジュールでは扱わず、後段の Better Auth
 * 内蔵 rate-limit / backend 側に任せる。
 *
 * Redis client (getRateLimitRedisClient) は auth.ts の Better Auth
 * `rateLimit.customStorage` と共有する。frontend 内で同一 REDIS_URL_RL
 * instance を 1 client で扱うことで、Better Auth 経路 (baRateLimit:* key) と
 * proxy.ts 経路 (rl:ip:* key) が論理分離されたまま接続コストを節約する
 * (red-team chain ι 解消の基盤)。
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

// HMR / vitest module reset で client が複数生成されないよう globalThis にぶら下げる
const globalForRedis = globalThis as unknown as {
  __vectorRateLimitRedis?: RedisClientType;
  __vectorRateLimitErrorLastMs?: number;
};

function logRedisError(context: string, err: unknown): void {
  // 60 秒ごとに 1 度だけ warn を出す。一度きりログにすると Redis 永続障害中に
  // 運用が気付けず fail-open が長時間続く事故になるため、window-bounded で
  // 継続出力する (red-team F23 対策)。
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
 * 本モジュールの `checkRateLimit` (proxy.ts sliding window log) と
 * `auth.ts` の Better Auth `rateLimit.customStorage` で共有する。
 * 戻り値 `null` は REDIS_URL_RL / REDIS_URL いずれも未設定の状況で、
 * 呼び出し側は fail-open (allow) の判定に使う (ADR-006 §3)。
 */
export function getRateLimitRedisClient(): RedisClientType | null {
  if (globalForRedis.__vectorRateLimitRedis) {
    return globalForRedis.__vectorRateLimitRedis;
  }
  // red-team C9 対策: 既存 REDIS_URL は backend taskiq broker と同 instance のため、
  // rate-limit ZSET の key 増殖が backend を道連れ shutdown させる構造リスクがある。
  // REDIS_URL_RL を優先し、空文字列 / undefined のときは REDIS_URL にフォールバック
  // (managed Redis 単一 instance 構成への退避経路)。空文字列を「明示的に値」と
  // 解釈する用途は想定しないため `||` で unset と等価扱いとする。
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
