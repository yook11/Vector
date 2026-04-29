// Better Auth CLI (`@better-auth/cli migrate`) からのみ読まれる schema 定義用
// config。runtime からは絶対に import しない。
//
// なぜ別ファイルか:
//   - `./auth.ts` は `import "server-only"` を持ち、CLI が ESM resolution する
//     際にサーバ用の guard で fail する (公式 README の制約)。
//   - server-only を外すと client component から間違って auth インスタンスを
//     掴めてしまい security guard が崩れるため、runtime ファイルは触らない。
//
// 重複の維持責任:
//   - 本ファイルの betterAuth(...) 引数は `./auth.ts` と完全一致させること。
//   - schema に効くフィールド (database / emailAndPassword / user
//     additionalFields / session / advanced.database) を変更したら必ず両方を
//     更新する。drift すると CI の `npx @better-auth/cli migrate` で生成される
//     schema が runtime と乖離する。

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { v7 as uuidv7 } from "uuid";
import { requireEnv } from "@/lib/env";

const pool = new Pool({
  connectionString: requireEnv("AUTH_DATABASE_URL"),
});

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
    cookieCache: { enabled: false },
  },
  trustedOrigins: [requireEnv("BETTER_AUTH_URL")],
  advanced: {
    database: {
      generateId: () => uuidv7(),
    },
  },
});
