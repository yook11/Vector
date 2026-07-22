// Better Auth 内蔵ログイン limiter の設定。runtime (auth.ts) と migration
// (auth.cli.ts) で同一設定を共有し、保存先を DB に固定する。
//
// なぜ DB storage か:
//   従来は Redis customStorage を使っていたが、Better Auth は storage.get の
//   失敗を try/catch せず null=「前科なし=許可」と判定する。Redis 障害時に
//   ログイン試行制限が無制限に fail-open する穴があった (OWASP API2:2023 は
//   認証 brute-force 制限を fail-open させるなと明言)。storage:"database" で
//   カウンターを Postgres (既存 auth schema) に置けば、Redis とは別の failure
//   domain になり、この穴が構造的に発生しなくなる。詳細は ADR-007 を参照。
//
// なぜ共有モジュールか:
//   storage:"database" が migration 側 (auth.cli.ts) にも無いと
//   `better-auth migrate` が auth.rateLimit テーブルを生成しない。runtime と
//   migration で設定が drift しないよう単一の SSoT に集約する。
//
// NOTE: このモジュールに `import "server-only"` を付けないこと。auth.cli.ts は
//   better-auth CLI (素の Node プロセス、Next.js server runtime ではない) から
//   import するため、server-only を含むと migrate が起動時に落ちる。中身は
//   秘匿値も server API も持たない純粋な設定オブジェクトなので server-only は不要。

// Better Auth runtime、schema CLI、provisioning input が共有する client-safe な
// password 長の契約。hash 実装や秘匿値は含めない。
export const passwordPolicy = {
  minLength: 8,
  maxLength: 128,
} as const;

export const authRateLimit = {
  enabled: true,
  storage: "database" as const,
  customRules: {
    "/sign-in/email": { window: 60, max: 5 },
    "/sign-up/email": { window: 60, max: 5 },
    "/reset-password": { window: 60, max: 3 },
  },
};
