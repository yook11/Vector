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
  type RedisIamConnection,
  redisIamConnectionOptions,
} from "@/lib/auth/redis-iam-auth";
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
  __vectorRateLimitFailOpenLastMs?: Record<string, number>;
  __vectorRateLimitMisconfigLogged?: boolean;
  __vectorClientIpTrustUnconfiguredLogged?: boolean;
  __vectorXffChainWindow?: XffChainWindow;
};

type XffChainWindow = {
  startedAtMs: number;
  xffRequestCount: number;
  multiValueXffRequestCount: number;
};

/**
 * XFF chain の観測を蓄積し、ウィンドウごとに件数を出す。
 *
 * ALB が XFF の 2 本目を素通しするかは AWS 未文書で、素通しなら末尾 (= 信頼している値)
 * が client 制御になる。多値 XFF の基底率が分かれば「多値を弾く」対処の巻き添え規模を
 * 実測値で判断できるため、挙動は変えず件数だけ測る。値そのものは PII になりうるので載せない。
 *
 * 分子が 0 でも出すのが recordRateLimitSignal との意図的な差。0 件とデータ無しを
 * 区別できないと基底率が読めない。率ではなく 2 数を出すのは、プロセス跨ぎ・ウィンドウ跨ぎの
 * 合算をログ基盤側に残すため。proxy にタイマーは置けないので経過判定は呼び出し時に行う
 * (無通信だとウィンドウは 60 秒より延びるが、件数対件数なので比は歪まない)。
 */
export function recordXffChainObservation(isMultiValue: boolean): void {
  const now = Date.now();
  const window: XffChainWindow = globalForRedis.__vectorXffChainWindow ?? {
    startedAtMs: now,
    xffRequestCount: 0,
    multiValueXffRequestCount: 0,
  };
  globalForRedis.__vectorXffChainWindow = window;
  window.xffRequestCount += 1;
  if (isMultiValue) {
    window.multiValueXffRequestCount += 1;
  }
  if (now - window.startedAtMs < ERROR_LOG_INTERVAL_MS) return;
  logServerEvent("warn", "frontend_xff_chain_observed", {
    xffRequestCount: window.xffRequestCount,
    multiValueXffRequestCount: window.multiValueXffRequestCount,
  });
  window.startedAtMs = now;
  window.xffRequestCount = 0;
  window.multiValueXffRequestCount = 0;
}

/**
 * CLIENT_IP_TRUST の未宣言を記録する。env 由来で不変なのでプロセスごとに 1 回。
 *
 * 経路異常 (missing_ip) と分けるのは、fail-closed を選んだ理由が「設定漏れで偽装穴が
 * 静かに残らない」ことだから。両者が同じ信号だと、運用者は edge 障害か deploy 設定漏れか
 * 判別できない。生の env 値は載せない。
 */
export function recordClientIpTrustUnconfigured(
  detail: "unset" | "invalid",
): void {
  if (globalForRedis.__vectorClientIpTrustUnconfiguredLogged) return;
  globalForRedis.__vectorClientIpTrustUnconfiguredLogged = true;
  logServerEvent("warn", "frontend_client_ip_trust_unconfigured", { detail });
}

// IAM 認証の誤設定は env 由来で不変なので、診断 message はプロセスごとに 1 回だけ
// 出す (継続的な可視化は checkRateLimit 側の unconfigured warn が担う)。
function logRedisIamMisconfigOnce(err: unknown): void {
  if (globalForRedis.__vectorRateLimitMisconfigLogged) return;
  globalForRedis.__vectorRateLimitMisconfigLogged = true;
  logServerEvent("warn", "frontend_rate_limit_redis_misconfigured", {
    detail: err instanceof Error ? err.message : String(err),
  });
}

function logRedisClientError(): void {
  const now = Date.now();
  const last = globalForRedis.__vectorRateLimitErrorLastMs;
  if (last !== undefined && now - last < ERROR_LOG_INTERVAL_MS) {
    return;
  }
  logServerEvent("warn", "frontend_rate_limit_redis_client_error");
  globalForRedis.__vectorRateLimitErrorLastMs = now;
}

export type RateLimitRequestClass = "sse" | "read" | "mutation";
type RateLimitFailOpenError = "unconfigured" | "connect" | "eval";

function recordRateLimitFailOpen(
  requestClass: RateLimitRequestClass,
  errorType: RateLimitFailOpenError,
): void {
  const now = Date.now();
  const store = globalForRedis.__vectorRateLimitFailOpenLastMs ?? {};
  globalForRedis.__vectorRateLimitFailOpenLastMs = store;
  const key = `${requestClass}:${errorType}`;
  const last = store[key];
  if (last !== undefined && now - last < ERROR_LOG_INTERVAL_MS) return;
  store[key] = now;
  logServerEvent("warn", "frontend_rate_limit_redis_fail_open", {
    requestClass,
    errorType,
  });
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
  // IAM 認証の誤設定 (URL に user 無し / cache 名・region 欠落) は URL 未設定と
  // 同じ fail-open に倒す (呼び出し側の unconfigured warn で 60 秒ごとに可視化)。
  let connection: RedisIamConnection;
  try {
    connection = redisIamConnectionOptions(url);
  } catch (err) {
    logRedisIamMisconfigOnce(err);
    return null;
  }
  // credentialsProvider を undefined で渡さない。node-redis は URL の userinfo
  // からも provider を作るため、undefined の明示上書きは password 認証を壊しうる。
  const c = createClient({
    url: connection.url,
    ...(connection.credentialsProvider
      ? { credentialsProvider: connection.credentialsProvider }
      : {}),
  }) as RedisClientType;
  c.on("error", logRedisClientError);
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
  options: { requestClass?: RateLimitRequestClass } = {},
): Promise<RateLimitDecision> {
  if (plan.tiers.length === 0) {
    return { allowed: true };
  }
  const c = getRateLimitRedisClient();
  if (!c) {
    recordRateLimitFailOpen(options.requestClass ?? "read", "unconfigured");
    return { allowed: true };
  }
  let operation: RateLimitFailOpenError = "connect";
  try {
    if (!c.isOpen) {
      await c.connect();
    }
    operation = "eval";
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
  } catch {
    recordRateLimitFailOpen(options.requestClass ?? "read", operation);
    return { allowed: true };
  }
}
