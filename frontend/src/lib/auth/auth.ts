import "server-only";

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { v7 as uuidv7 } from "uuid";
import { poolConfigFromUrl } from "@/lib/auth/pool-ssl";
import { getRateLimitRedisClient } from "@/lib/auth/rate-limit";
import { requireEnv } from "@/lib/env";

const isProduction = process.env.NODE_ENV === "production";

// pg.Pool 設定明示 (red-team C8 / F12 対策、PR-F の rate limit と二重防御)。
//
// Better Auth は内部で auth スキーマに対し短い select / insert を撃つだけなので、
// 1 connection あたりの占有時間は ms オーダーが想定値。max=20 はその前提で
// 通常 traffic を捌ける幅 (compose Postgres `max_connections` default 100 のうち
// backend が大半を消費する事情を加味)。env override 化は YAGNI、必要になってから。
//
// connectionTimeoutMillis: 0 (default) は "Pool 取得を無限に待つ" 仕様で、
// PR-F の rate limit を擦り抜けるシナリオや過渡的な Pool 飽和時に request が
// ハングし続ける増幅源になる。5 秒 fail-fast に格下げして 5xx を即返す。
// statement_timeout は pg server side で query 自体も 5 秒で切る二重防御。
const pool = new Pool({
  // 接続文字列の sslmode を ssl オブジェクトに変換する (Neon は SSL 必須、
  // dev docker は SSL なし)。詳細は pool-ssl.ts を参照。
  ...poolConfigFromUrl(requireEnv("AUTH_DATABASE_URL")),
  max: 20,
  connectionTimeoutMillis: 5000,
  idleTimeoutMillis: 10_000,
  statement_timeout: 5000,
});

// Better Auth のクエリは全て 'auth' スキーマに向ける
pool.on("connect", (client: PoolClient) => {
  client.query("SET search_path TO auth, public");
});

// Better Auth 1.6.7 rate-limiter (`node_modules/better-auth/dist/api/rate-limiter/index.mjs`)
// が期待する customStorage の shape:
//   get(key) -> { count: number, lastRequest: number, key?: string } | null
//   set(key, value, _update?: boolean) -> void
//
// customStorage を渡すと storage 階層で最優先になり、secondaryStorage や
// rateLimit.storage の設定は無視される (= session 実体は DB のまま、cookieCache
// 無効維持と整合 / ADR-006 §2)。これにより chain ι (cold-start 時の memory
// storage 揮発) を解消しつつ、session 設計には影響しない。
//
// fail-open: Redis 不通時は get で null を返し set を skip する (ADR-006 §3)。
// 一次防衛線は proxy.ts の sliding window log + pg.Pool 設定 (max=20 +
// connectionTimeoutMillis=5s + statement_timeout=5s) に委ねる。
//
// TTL は全 entry 一律 600s (10 分)。最大 window は 60s (customRules) +
// default special rule 60s (`/forget-password*` 等) なので余裕あり。
// stale entry が残っても次の request で count=1 にリセットされるため誤動作なし。
// Better Auth `onResponseRateLimit` は常に `key` を含めて set を呼ぶため
// (`node_modules/better-auth/dist/api/rate-limiter/index.mjs:169-183`)、
// `key` は required で型を定義する。
type RateLimitEntry = {
  key: string;
  count: number;
  lastRequest: number;
};

const rateLimitCustomStorage = {
  async get(key: string): Promise<RateLimitEntry | null> {
    const c = getRateLimitRedisClient();
    if (!c) return null;
    try {
      if (!c.isOpen) await c.connect();
      const raw = await c.get(`baRateLimit:${key}`);
      return raw ? (JSON.parse(raw) as RateLimitEntry) : null;
    } catch {
      // fail-open (ADR-006 §3): rate-limit storage 障害は throttle skip で吸収
      return null;
    }
  },
  async set(key: string, value: RateLimitEntry): Promise<void> {
    const c = getRateLimitRedisClient();
    if (!c) return;
    try {
      if (!c.isOpen) await c.connect();
      await c.set(`baRateLimit:${key}`, JSON.stringify(value), { EX: 600 });
    } catch {
      // fail-open
    }
  },
};

export const auth = betterAuth({
  database: pool,
  basePath: "/api/auth",
  emailAndPassword: {
    enabled: true,
    minPasswordLength: 8,
  },
  user: {
    additionalFields: {
      role: {
        type: "string",
        defaultValue: "user",
        input: false,
      },
    },
  },
  session: {
    // cookieCache は無効化: 有効にすると最大 maxAge 秒間 stale な role / session が
    // 認可判定に使われ、admin 降格や session revoke の即時反映が効かなくなる。
    cookieCache: { enabled: false },
  },
  trustedOrigins: [requireEnv("BETTER_AUTH_URL")],
  // Better Auth 内蔵 rate-limit (`/api/auth/*` HTTP router にのみ適用)。
  // proxy.ts の application-level rate-limit (`/api/*` 全域) との二段防御 (ADR-006 §1)。
  //
  // customStorage で frontend の Redis client (REDIS_URL_RL) を再利用 → chain ι 解消。
  // customRules の path は normalizePathname で basePath strip 済 (`/api/auth` を含めない)。
  // default special rule で /sign-in* /sign-up* /change-* は 3 req/10 sec、
  // /forget-password* /request-password-reset 等は 3 req/60 sec に絞られているが、
  // 以下は default より window を伸ばし credential stuffing を抑制しつつ正規 user の
  // UX を改善する (PR #408 の identifier fail-closed と組み合わせて red-team chain α
  // の Better Auth 側 bypass を構造的に閉じる)。
  rateLimit: {
    enabled: true,
    customStorage: rateLimitCustomStorage,
    customRules: {
      "/sign-in/email": { window: 60, max: 5 },
      "/sign-up/email": { window: 60, max: 5 },
      "/reset-password": { window: 60, max: 3 },
    },
  },
  advanced: {
    // red-team chain α / CF-22 構造防御:
    //   Better Auth `getIp` (node_modules/better-auth/dist/utils/get-request-ip.mjs)
    //   は `ipAddressHeaders` を先頭から走査して最初に valid IP を返した header を
    //   採用する。production では Fly Edge が必ず付与する `Fly-Client-IP` のみを
    //   trusted source とする (XFF 詐称経路を完全に閉じる、ADR-006 §4 / proxy.ts の
    //   identifier fail-closed と対称)。fly-client-ip 不在時は getIp が null を返し
    //   Better Auth rate-limit は skip されるが、proxy.ts (`/api/*` matcher) が
    //   unknown bucket で先に throttle するため一次防衛は維持される。
    //   dev / test は Fly Edge を経由しないため fly-client-ip → x-forwarded-for の
    //   fallback を許容する。
    ipAddress: {
      ipAddressHeaders: isProduction
        ? ["fly-client-ip"]
        : ["fly-client-ip", "x-forwarded-for"],
      disableIpTracking: false,
    },
    database: {
      // UUIDv7 (時刻順) を採用。crypto.randomUUID() の v4 はランダム順で
      // B-tree index が断片化するため、書込負荷下の性能を見据えて v7 を選択。
      generateId: () => uuidv7(),
    },
  },
});
