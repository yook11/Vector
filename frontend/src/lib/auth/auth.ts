import "server-only";

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { v7 as uuidv7 } from "uuid";
import { requireEnv } from "@/lib/api/internal-config";

const pool = new Pool({
  connectionString: requireEnv("AUTH_DATABASE_URL"),
});

// Better Auth のクエリは全て 'auth' スキーマに向ける
pool.on("connect", (client: PoolClient) => {
  client.query("SET search_path TO auth, public");
});

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
  advanced: {
    database: {
      // UUIDv7 (時刻順) を採用。crypto.randomUUID() の v4 はランダム順で
      // B-tree index が断片化するため、書込負荷下の性能を見据えて v7 を選択。
      generateId: () => uuidv7(),
    },
  },
});
