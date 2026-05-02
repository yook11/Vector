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
 * 化し、`INTERNAL_API_SECRET` がフロント bundle に滲み出す事故を構造的に防ぐ。
 */

import "server-only";

import { SignJWT } from "jose";
import { narrowRole } from "@/lib/auth/role";
import type { Session } from "@/lib/auth/session";
import { requireEnv } from "@/lib/env";

export const INTERNAL_API_URL = requireEnv("INTERNAL_API_URL");

const INTERNAL_API_SECRET = requireEnv(
  "INTERNAL_API_SECRET",
  "generate one with `openssl rand -hex 32`",
);

// HS256 署名鍵: モジュール読込時に 1 度だけバイト列化してキャッシュする。
// secret 文字列は >=32 バイト (backend Settings._validate_internal_api_secret で保証)。
const INTERNAL_JWT_SIGNING_KEY = new TextEncoder().encode(INTERNAL_API_SECRET);

const INTERNAL_JWT_ALGORITHM = "HS256";
const INTERNAL_JWT_TTL = "60s";
// iss / aud は backend (`backend/app/dependencies.py`) と同じ literal を要求。
// INTERNAL_API_SECRET 漏洩時に「Vector の文脈で署名された JWT」を強制する二重防御。
const INTERNAL_JWT_ISSUER = "vector-bff";
const INTERNAL_JWT_AUDIENCE = "vector-backend";

/**
 * BFF→backend 間の認証 JWT を 1 リクエスト分だけ発行する。
 *
 * Better Auth セッションから `user.id` / `user.role` を取り出し HS256 で署名。
 * backend は同じ INTERNAL_API_SECRET で検証する (`backend/app/dependencies.py`)。
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
