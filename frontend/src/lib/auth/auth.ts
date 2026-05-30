import "server-only";

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { v7 as uuidv7 } from "uuid";
import { poolConfigFromUrl } from "@/lib/auth/pool-ssl";
import { getRateLimitRedisClient } from "@/lib/auth/rate-limit";
import { requireEnv } from "@/lib/env";

const isProduction = process.env.NODE_ENV === "production";

// Better Auth 用 pg.Pool。Pool 取得待ちと query を 5 秒で止め、
// request 滞留や接続枯渇の増幅を抑える。
// max=20 は frontend auth 用に明示し、backend pool と接続上限を分けて扱う。
const pool = new Pool({
  // sslmode は pool-ssl.ts で Neon/dev docker に合わせて変換する。
  ...poolConfigFromUrl(requireEnv("AUTH_DATABASE_URL")),
  max: 20,
  connectionTimeoutMillis: 5000,
  idleTimeoutMillis: 10_000,
  statement_timeout: 5000,
});

// Better Auth のクエリは全て 'auth' スキーマに向ける。
pool.on("connect", (client: PoolClient) => {
  client.query("SET search_path TO auth, public");
});

// Better Auth rate-limiter が期待する customStorage の shape:
//   get(key) -> { count: number, lastRequest: number, key?: string } | null
//   set(key, value, _update?: boolean) -> void
//
// customStorage は rateLimit storage として最優先され、session 実体は DB のまま。
// Redis 不通時は get=null/set skip の fail-open とし、proxy 層の rate-limit と
// pg.Pool timeout に一次防衛を任せる。TTL は最大 window より長い 600s。
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
      // fail-open: rate-limit storage 障害は throttle skip で吸収する。
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
  // Better Auth 内蔵 rate-limit は `/api/auth/*` router 専用。
  // proxy.ts の全域 rate-limit と分担し、credential stuffing 系の window を伸ばす。
  // customRules の path は normalizePathname で basePath strip 済。
  // `/api/auth` は含めない。
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
    // production では Fly Edge が付与する fly-client-ip だけを
    // trusted source にする。
    // 欠如時は Better Auth 側の IP rate-limit は skip されるが、
    // proxy.ts が unknown bucket で先に throttle する。
    // dev/test は Fly Edge を経由しないため x-forwarded-for fallback を許可する。
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
