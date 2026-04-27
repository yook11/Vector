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

export function requireEnv(name: string, hint?: string): string {
  const value = process.env[name];
  if (!value) {
    const suffix = hint ? `; ${hint}` : "";
    throw new Error(`${name} is required${suffix}`);
  }
  return value;
}

export const INTERNAL_API_URL = requireEnv("INTERNAL_API_URL");

export const INTERNAL_API_SECRET = requireEnv(
  "INTERNAL_API_SECRET",
  "generate one with `openssl rand -hex 32`",
);

/**
 * Better Auth Session の最低限の shape。
 * `role` は `additionalFields` で追加されているため型上は明示できず、
 * 内部で安全に narrow する。型安全な session 拡張は別タスク (role 型拡張) で実施。
 */
type SessionLike = { user: { id: string } };

/**
 * Build the auth headers required by the backend internal API:
 * X-User-ID / X-User-Role / X-Internal-Secret.
 */
export function buildInternalAuthHeaders(
  session: SessionLike,
): Record<string, string> {
  const role =
    ((session.user as Record<string, unknown>).role as string | undefined) ??
    "user";
  return {
    "X-User-ID": session.user.id,
    "X-User-Role": role,
    "X-Internal-Secret": INTERNAL_API_SECRET,
  };
}
