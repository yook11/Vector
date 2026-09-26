// pg.Pool 用に接続文字列から SSL 設定を分離するヘルパー。
//
// node-postgres は接続文字列の `sslmode` と Pool の `ssl` オブジェクトを併用
// すると互いを上書きし合う既知問題 (brianc/node-postgres#3355) があるため、
// `sslmode` を URL から取り除き、`ssl` オブジェクトへ明示変換する。
//
// 本番 (RDS) の接続文字列は `sslmode=require` を含め、dev (docker 同一ネットワーク)
// は持たないため、接続文字列のみで dev / 本番の SSL 要否を切り替えられる
// (CLAUDE.md の「env に集約」方針と整合)。
//
// SSL を使う場合は verify-full 相当 (CA + ホスト名検証) を rejectUnauthorized
// で強制し MITM を防ぐ。
//
// runtime (auth.ts) からも CLI (auth.cli.ts) からも import されるため、
// `server-only` guard は持たせない (pg と node の標準 module だけに依存する)。

import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { PoolConfig } from "pg";

// RDS の regional root 3 本 (backend/app/db と同一で、test_db_ssl が固定する)。
// `ca` は既定の信頼集合を置き換えるので、DB 接続はこの root だけを信頼する。
// 実行ディレクトリ直下に置く (image では /app、手元では frontend/)。
const RDS_CA_BUNDLE_FILE = "rds-ca-ap-northeast-1.pem";

export function poolConfigFromUrl(rawUrl: string): PoolConfig {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error("Invalid database connection URL.");
  }
  const sslmode = url.searchParams.get("sslmode");
  // pg には sslmode を渡さない (#3355 回避)。
  url.searchParams.delete("sslmode");

  const ssl =
    sslmode && sslmode !== "disable"
      ? {
          ca: readFileSync(join(process.cwd(), RDS_CA_BUNDLE_FILE), "utf8"),
          rejectUnauthorized: true,
        }
      : false;

  return { connectionString: url.toString(), ssl };
}
