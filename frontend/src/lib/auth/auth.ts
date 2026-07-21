import "server-only";

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { v7 as uuidv7 } from "uuid";
import { authRateLimit } from "@/lib/auth/auth-config";
import { poolConfigFromUrl } from "@/lib/auth/pool-ssl";
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

export const auth = betterAuth({
  database: pool,
  basePath: "/api/auth",
  emailAndPassword: {
    enabled: true,
    disableSignUp: true,
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
  // 保存先は DB (auth schema)。設定は auth-config.ts に集約し migration と共有する。
  // customRules の path は normalizePathname で basePath strip 済 (`/api/auth` は含めない)。
  rateLimit: authRateLimit,
  advanced: {
    // production では Fly Edge が付与する fly-client-ip だけを
    // trusted source にする。
    // 欠如時は Better Auth 側の IP rate-limit は skip されるが、
    // proxy.ts 側でも IP 未解決は identity でなく経路異常として扱う
    // (read/_rsc は fail-open、anon mutation のみ rl:uwrite:global で縛る / ADR-009)。
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
