/**
 * RDS IAM 認証: app runtime の pg.Pool に、接続ごとの auth token を差し込む。
 *
 * password を持たない接続文字列 (AWS) では認証情報を接続のたびに IAM から作る。
 * token は 15 分で失効するので Pool 生成時に 1 度作るのでは足りず、pg の
 * `password` に関数 (`() => Promise<string>`) を渡して接続確立ごとに作らせる。
 *
 * 署名はローカルで完結し、credential の取得・更新だけが HTTP。`Signer` は credential
 * の残り寿命が 15 分を切っていたら強制 refresh する (presigned URL は署名に使った
 * credential より長生きできないため)。
 *
 * **射程は app runtime の接続だけ。** Better Auth CLI (`auth.cli.ts`) は
 * `poolConfigFromUrl` を直接使い、password 認証を続ける。backend の
 * `create_runtime_engine` / `create_app_engine` の分け方と同じ境界。
 *
 * `import "server-only"` で client bundle への混入を build error 化する
 * (CLI から import できないことが、上の射程の構造的な裏付けにもなる)。
 */

import "server-only";

import { Signer } from "@aws-sdk/rds-signer";
import type { PoolConfig } from "pg";
import { poolConfigFromUrl } from "@/lib/auth/pool-ssl";

// token は host:port ごとに署名するため、URL に port が無い場合の既定が要る。
const DEFAULT_POSTGRES_PORT = 5432;

// region と credential は AWS SDK の解決規則に任せる (ECS では task role と AWS_REGION)。
function buildAuthTokenProvider(url: URL): () => Promise<string> {
  // error message に URL を載せない (password が混じる経路を作らない)。
  if (!url.hostname) {
    throw new Error(
      "an RDS auth token requires a host in the connection string",
    );
  }
  const username = decodeURIComponent(url.username);
  if (!username) {
    throw new Error(
      "an RDS auth token requires a user in the connection string " +
        "(the token is signed per database user)",
    );
  }
  const signer = new Signer({
    hostname: url.hostname,
    port: url.port ? Number(url.port) : DEFAULT_POSTGRES_PORT,
    username,
  });
  return () => signer.getAuthToken();
}

/**
 * app runtime 用の PoolConfig を返す。IAM 認証が有効なら token 生成器を差し込む。
 *
 * 無効時は `poolConfigFromUrl` と同じで、URL の password がそのまま使われる。
 */
export function runtimePoolConfigFromUrl(rawUrl: string): PoolConfig {
  const config = poolConfigFromUrl(rawUrl);
  if (process.env.DB_IAM_AUTH !== "true") {
    return config;
  }
  // URL の妥当性は poolConfigFromUrl が先に検証している (不正なら redact 済で throw)。
  //
  // pg は Client 生成時に connectionString を再解析し、その結果 (password の無い
  // URL なら null) で明示指定の password を上書きするため、生成器を生かすには
  // connectionString を渡さず個別フィールドへ分解する必要がある。search_path 等の
  // query param はここで落ちる (schema は authPool の on connect が担う)。
  const url = new URL(rawUrl);
  return {
    host: url.hostname,
    port: url.port ? Number(url.port) : DEFAULT_POSTGRES_PORT,
    database: decodeURIComponent(url.pathname.slice(1)),
    user: decodeURIComponent(url.username),
    ssl: config.ssl,
    password: buildAuthTokenProvider(url),
  };
}
