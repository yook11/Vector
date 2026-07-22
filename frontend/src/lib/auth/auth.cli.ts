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
//   - 本ファイルの betterAuth(...) 引数は schema に効く範囲で `./auth.ts` と
//     一致させること。具体的には: database / emailAndPassword /
//     user.additionalFields / session の各フィールド。
//   - schema 非依存の認証モードも runtime との drift を防ぐため object 単位で同期する。
//   - rateLimit は schema (auth.rateLimit テーブル) に効くため `auth-config.ts`
//     に集約し runtime と import 共有する (手動同期不要)。
//   - 例外として `advanced.database.generateId` は意図的に異なる値を持つ:
//       * runtime (auth.ts): `() => uuidv7()` で UUID v7 (時刻順) を生成
//       * CLI (本ファイル):  `"uuid"` 文字列で Better Auth CLI に uuid 列型
//         での schema 生成を指示する (CLI は generateId === "uuid" でしか
//         postgres uuid 型を選択しない、`get-migration.mjs:185` 参照)
//     uuid 列は v7 文字列も受け取れるため runtime と整合する。

import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";
import { authRateLimit } from "@/lib/auth/auth-config";
import { poolConfigFromUrl } from "@/lib/auth/pool-ssl";
import { requireEnv } from "@/lib/env";

// CLI からも Neon (SSL 必須) に migrate するため、runtime (auth.ts) と同じ
// SSL 変換を通す。詳細は pool-ssl.ts を参照。
const pool = new Pool(poolConfigFromUrl(requireEnv("AUTH_DATABASE_URL")));

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
    cookieCache: { enabled: false },
  },
  trustedOrigins: [requireEnv("BETTER_AUTH_URL")],
  // rateLimit.storage:"database" を CLI 側にも渡し、migrate が auth.rateLimit
  // テーブルを生成するようにする (storage が database でないとテーブルは作られない)。
  // 設定は runtime (auth.ts) と auth-config.ts で共有する。
  rateLimit: authRateLimit,
  advanced: {
    database: {
      // 文字列 "uuid" を渡すと CLI は uuid 列型で schema を生成する。runtime
      // は auth.ts 側の uuidv7() で実 ID を生成するが、uuid 列はその出力 (v7)
      // も accept するため整合する。auth.ts の関数 generateId 経路だと CLI 側
      // では `text` になってしまう (上記 docstring 参照)。
      generateId: "uuid",
    },
  },
});
