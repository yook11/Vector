/**
 * BFF とバックエンド間の内部 API 接続設定 + 認可ヘッダ生成。
 *
 * `lib/api/server-fetcher.ts` (Server Component / Server Action から呼ぶ
 * fetcher) から参照される共通設定。
 *
 * デフォルト値や `??` フォールバックは持たせない方針 — 未設定時はモジュール
 * 読込時に throw して fail-fast にする (build / 起動時に発覚させる)。
 *
 * `import "server-only"` で client component からの誤 import を build error
 * 化し、`BFF_JWT_SIGNING_SECRET` がフロント bundle に滲み出す事故を構造的に防ぐ。
 */

import "server-only";

import { SignJWT } from "jose";
import { narrowRole } from "@/lib/auth/role";
import type { Session } from "@/lib/auth/session";
import { requireEnv } from "@/lib/env";

// BFF→backend の JWT を攻撃者制御 host に送らないため、内部 API host を絞る。
const _ALLOWED_INTERNAL_API_HOSTS = new Set([
  "localhost",
  "127.0.0.1",
  "backend",
]);
const _ALLOWED_INTERNAL_API_HOST_SUFFIX = ".flycast";

/**
 * INTERNAL_API_URL の host を全環境 allowlist + production narrowing で検証する。
 *
 * 全環境共通 (global allowlist): localhost / 127.0.0.1 / backend (compose DNS)
 * または *.flycast (Fly private network) を許可。
 *
 * production narrowing (NODE_ENV="production"): dev host は本番で到達不能なため
 * *.flycast 以外を fail-closed で拒否する (backend の
 * _enforce_flycast_in_production と対称)。
 *
 * `nodeEnv` を引数化することでテストでは env を tampering せず純粋関数として
 * 検証できる (default は `process.env.NODE_ENV`)。
 */
export function assertAllowedInternalApiUrl(
  rawUrl: string,
  nodeEnv: string | undefined = process.env.NODE_ENV,
): void {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error(`INTERNAL_API_URL is not a valid URL: ${rawUrl}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(
      `INTERNAL_API_URL must use http or https scheme, got ${parsed.protocol}`,
    );
  }
  const host = parsed.hostname;
  const isAllowed =
    _ALLOWED_INTERNAL_API_HOSTS.has(host) ||
    host.endsWith(_ALLOWED_INTERNAL_API_HOST_SUFFIX);
  if (!isAllowed) {
    throw new Error(
      `INTERNAL_API_URL host "${host}" is not an allowed internal destination; ` +
        "expected localhost / 127.0.0.1 / backend (compose) or a *.flycast host (Fly private network)",
    );
  }
  if (
    nodeEnv === "production" &&
    !host.endsWith(_ALLOWED_INTERNAL_API_HOST_SUFFIX)
  ) {
    throw new Error(
      `in production INTERNAL_API_URL must be a *.flycast host (Fly private network), got host "${host}"`,
    );
  }
}

function _loadInternalApiUrl(): string {
  const url = requireEnv("INTERNAL_API_URL");
  assertAllowedInternalApiUrl(url);
  return url;
}

export const INTERNAL_API_URL = _loadInternalApiUrl();

// BFF→backend JWT 署名鍵。backend は同じ secret で検証する。
const BFF_JWT_SIGNING_SECRET = requireEnv(
  "BFF_JWT_SIGNING_SECRET",
  "generate one with `openssl rand -hex 32`",
);

// HS256 署名鍵: モジュール読込時に 1 度だけバイト列化してキャッシュする。
// secret 文字列は >=32 バイト (backend Settings._assert_strong_secret で保証)。
const INTERNAL_JWT_SIGNING_KEY = new TextEncoder().encode(
  BFF_JWT_SIGNING_SECRET,
);

const INTERNAL_JWT_ALGORITHM = "HS256";
const INTERNAL_JWT_TTL = "60s";
// iss / aud は backend (`backend/app/dependencies.py`) と同じ literal を要求。
// secret 漏洩時に「Vector の文脈で署名された JWT」を強制する二重防御。
const INTERNAL_JWT_ISSUER = "vector-bff";
const INTERNAL_JWT_AUDIENCE = "vector-backend";

/**
 * BFF→backend 間の認証 JWT を 1 リクエスト分だけ発行する。
 *
 * Better Auth セッションから `user.id` / `user.role` を取り出し HS256 で署名。
 * backend は同じ BFF_JWT_SIGNING_SECRET で検証する (`backend/app/dependencies.py`)。
 * 有効期限を 60 秒に絞ることで、secret 漏洩時の悪用ウィンドウを構造的に短縮する。
 */
export async function buildInternalAuthHeaders(
  session: Session,
): Promise<Record<string, string>> {
  const role = narrowRole(session.user.role);
  const token = await new SignJWT({ role })
    .setProtectedHeader({ alg: INTERNAL_JWT_ALGORITHM })
    .setSubject(session.user.id)
    .setIssuer(INTERNAL_JWT_ISSUER)
    .setAudience(INTERNAL_JWT_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(INTERNAL_JWT_TTL)
    .sign(INTERNAL_JWT_SIGNING_KEY);
  return { Authorization: `Bearer ${token}` };
}

/**
 * user-less な BFF 経由証明 JWT を発行する (sub/role 無し)。
 *
 * 「正規 BFF から来た」ことだけを証明し、ログイン済みかは表現しない。session を
 * 取らず署名鍵と時刻だけで作るため cookies()/headers() を踏まず、`"use cache"`
 * 内の anon read からも安全に付与できる。backend の require_bff_request が
 * 同じ secret/iss/aud で検証する。
 */
export async function buildBffRequestHeaders(): Promise<
  Record<string, string>
> {
  const token = await new SignJWT({})
    .setProtectedHeader({ alg: INTERNAL_JWT_ALGORITHM })
    .setIssuer(INTERNAL_JWT_ISSUER)
    .setAudience(INTERNAL_JWT_AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(INTERNAL_JWT_TTL)
    .sign(INTERNAL_JWT_SIGNING_KEY);
  return { Authorization: `Bearer ${token}` };
}
