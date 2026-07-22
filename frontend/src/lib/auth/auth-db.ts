import "server-only";

import type { PoolClient } from "pg";
import { Pool } from "pg";
import { poolConfigFromUrl } from "@/lib/auth/pool-ssl";
import { requireEnv } from "@/lib/env";

// Better Auth 用 pg.Pool。Pool 取得待ちと query を 5 秒で止め、
// request 滞留や接続枯渇の増幅を抑える。
// max=20 は frontend auth 用に明示し、backend pool と接続上限を分けて扱う。
export const authPool = new Pool({
  // sslmode は pool-ssl.ts で Neon/dev docker に合わせて変換する。
  ...poolConfigFromUrl(requireEnv("AUTH_DATABASE_URL")),
  max: 20,
  connectionTimeoutMillis: 5000,
  idleTimeoutMillis: 10_000,
  statement_timeout: 5000,
});

// Better Auth のクエリは全て 'auth' スキーマに向ける。
authPool.on("connect", (client: PoolClient) => {
  client.query("SET search_path TO auth, public");
});
