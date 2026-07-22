// pg.Pool 用に接続文字列から SSL 設定を分離する純粋ヘルパー。
//
// node-postgres は接続文字列の `sslmode` と Pool の `ssl` オブジェクトを併用
// すると互いを上書きし合う既知問題 (brianc/node-postgres#3355) があるため、
// `sslmode` (および pg 非対応の `channel_binding`) を URL から取り除き、`ssl`
// オブジェクトへ明示変換する。
//
// Neon 等の managed Postgres は接続文字列に `sslmode=require` を含める一方、
// dev (docker 同一ネットワーク) は持たないため、接続文字列のみで dev / 本番の
// SSL 要否を切り替えられる (CLAUDE.md の「env に集約」方針と整合)。
//
// SSL を使う場合は verify-full 相当 (CA + ホスト名検証) を rejectUnauthorized
// で強制し MITM を防ぐ。Fly.io → Neon は public internet を通るため検証は必須。
// Neon の証明書は標準 CA (Let's Encrypt) なので追加 root 証明書は要らない。
//
// runtime (auth.ts) からも CLI (auth.cli.ts) からも import されるため、
// `server-only` guard は持たせない (pg の型のみに依存する純粋関数)。

import type { PoolConfig } from "pg";

export function poolConfigFromUrl(rawUrl: string): PoolConfig {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error("Invalid database connection URL.");
  }
  const sslmode = url.searchParams.get("sslmode");
  // pg には sslmode / channel_binding を渡さない (#3355 回避 + 未対応 param 排除)。
  url.searchParams.delete("sslmode");
  url.searchParams.delete("channel_binding");

  const ssl =
    sslmode && sslmode !== "disable" ? { rejectUnauthorized: true } : false;

  return { connectionString: url.toString(), ssl };
}
