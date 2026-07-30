/**
 * ElastiCache IAM 認証: rate-limit 用 Redis 接続に、接続ごとの auth token を差し込む。
 *
 * RDS (`db-iam-auth.ts`) と同型だが、ElastiCache には `@aws-sdk/rds-signer` に相当する
 * 公式 signer が無く、token は SigV4 presign の自前組み立てになる。
 * `https://<cache名>/?Action=connect&User=<user>` を service=elasticache で presign し、
 * scheme を落とした残り全体を AUTH の password として渡す。**署名の host は DNS
 * endpoint ではなく cache 名 (replication group id)** で、URL から導出できないため
 * env で明示的に受ける。
 *
 * token は 15 分で失効し、IAM 認証の接続は 12 時間で強制切断される。node-redis の
 * credentialsProvider は handshake (初回接続・再接続とも) のたびに呼ばれるため、
 * その口に載せることで「切断 → 自動再接続 → 新 token」が成立する。
 *
 * node-redis は password が空だと HELLO の AUTH 節ごと省略して default user で繋ぐ
 * ため、token を password として返すこと自体が IAM 認証の成立条件になる。
 */

import "server-only";

import { Sha256 } from "@aws-crypto/sha256-js";
import { fromNodeProviderChain } from "@aws-sdk/credential-providers";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import type { RedisClientOptions } from "redis";

type CredentialsProvider = NonNullable<
  RedisClientOptions["credentialsProvider"]
>;

export interface RedisIamConnection {
  url: string;
  credentialsProvider?: CredentialsProvider;
}

// AWS 側の token 有効期限が 15 分のため、presign もそれに合わせる。
const TOKEN_EXPIRES_IN_SECONDS = 900;

// SigV4 の canonical query と同じ RFC 3986 エンコード (encodeURIComponent が
// 素通しする sub-delims も % エンコードする)。
function escapeUri(value: string): string {
  return encodeURIComponent(value).replace(
    /[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function buildTokenSigner(
  user: string,
  cacheName: string,
  region: string,
): () => Promise<string> {
  const signer = new SignatureV4({
    service: "elasticache",
    region,
    credentials: fromNodeProviderChain(),
    sha256: Sha256,
  });
  return async () => {
    const presigned = await signer.presign(
      new HttpRequest({
        method: "GET",
        protocol: "https:",
        hostname: cacheName,
        path: "/",
        query: { Action: "connect", User: user },
        headers: { host: cacheName },
      }),
      { expiresIn: TOKEN_EXPIRES_IN_SECONDS },
    );
    const query = Object.entries(presigned.query ?? {})
      .flatMap(([key, value]) =>
        (Array.isArray(value) ? value : [value ?? ""]).map(
          (v) => `${escapeUri(key)}=${escapeUri(v)}`,
        ),
      )
      .join("&");
    return `${cacheName}/?${query}`;
  };
}

/**
 * 接続 URL と credentialsProvider を返す。
 *
 * IAM 認証が有効なら「どの user で繋ぐか」を URL から provider へ写し、userinfo を
 * 除いた URL を返す (URL が単一の情報源)。無効時は URL をそのまま使う
 * (dev / Fly の password 認証)。誤設定は throw するので、fail-open にするかは
 * 呼び出し側が決める。
 */
export function redisIamConnectionOptions(rawUrl: string): RedisIamConnection {
  if (process.env.REDIS_IAM_AUTH !== "true") {
    return { url: rawUrl };
  }
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    // Node の Invalid URL エラーは入力文字列を message に含みうるため redact する
    // (password が混じる経路を作らない)。
    throw new Error("the rate limit Redis URL is not a parsable URL");
  }
  const user = decodeURIComponent(url.username);
  if (!user) {
    throw new Error(
      "an ElastiCache auth token requires a user in the connection URL " +
        "(the token is signed per cache user)",
    );
  }
  const cacheName = process.env.REDIS_IAM_CACHE_NAME_RL;
  const region = process.env.AWS_REGION;
  if (!cacheName || !region) {
    throw new Error(
      "REDIS_IAM_AUTH requires REDIS_IAM_CACHE_NAME_RL and AWS_REGION " +
        "(the token is signed against the cache name, per region)",
    );
  }
  url.username = "";
  url.password = "";
  const signToken = buildTokenSigner(user, cacheName, region);
  return {
    url: url.toString(),
    credentialsProvider: {
      type: "async-credentials-provider",
      credentials: async () => ({
        username: user,
        password: await signToken(),
      }),
    },
  };
}
