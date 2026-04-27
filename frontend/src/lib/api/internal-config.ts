/**
 * BFF プロキシとバックエンド間の内部 API 接続設定 + 認可ヘッダ生成。
 *
 * `lib/api-client.ts` (Server Component から呼ぶ fetcher) と
 * `app/api/proxy/[...path]/route.ts` (BFF プロキシ Route Handler) の
 * 両方から参照される共通設定。
 *
 * デフォルト値や `??` フォールバックは持たせない方針 — 未設定時はモジュール
 * 読込時に throw して fail-fast にする (build / 起動時に発覚させる)。
 */

import { SignJWT } from "jose";
import type { Session } from "@/lib/auth/session";

export function requireEnv(name: string, hint?: string): string {
  const value = process.env[name];
  if (!value) {
    const suffix = hint ? `; ${hint}` : "";
    throw new Error(`${name} is required${suffix}`);
  }
  return value;
}

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
  const token = await new SignJWT({ role: session.user.role })
    .setProtectedHeader({ alg: INTERNAL_JWT_ALGORITHM })
    .setSubject(session.user.id)
    .setIssuedAt()
    .setExpirationTime(INTERNAL_JWT_TTL)
    .sign(INTERNAL_JWT_SIGNING_KEY);
  return { Authorization: `Bearer ${token}` };
}
