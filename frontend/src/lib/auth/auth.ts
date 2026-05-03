import "server-only";

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { v7 as uuidv7 } from "uuid";
import { requireEnv } from "@/lib/env";

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
  connectionString: requireEnv("AUTH_DATABASE_URL"),
  max: 20,
  connectionTimeoutMillis: 5000,
  idleTimeoutMillis: 10_000,
  statement_timeout: 5000,
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
