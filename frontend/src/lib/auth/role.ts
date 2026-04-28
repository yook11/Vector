/**
 * UserRole の許可リスト + narrowing。
 *
 * Better Auth `additionalFields.role` の TypeScript 型は `string` で将来追加
 * され得るため、認可境界 (admin layout / BFF→backend JWT 署名) ではこの
 * allowlist を経由した narrowing を必ず通す。
 *
 * backend `UserRole` (`backend/app/dependencies.py`) と整合させる。乖離する
 * と JWT に backend が知らない role を載せてしまう / 逆に admin 判定が通らな
 * いといった事故が起きるので、追加時は backend と同時に更新すること。
 */

export const ALLOWED_ROLES = ["user", "admin"] as const;

export type UserRole = (typeof ALLOWED_ROLES)[number];

/**
 * 任意文字列を `UserRole` に narrowing する。
 *
 * allowlist に無い値は `"user"` に縮退 (fail-safe)。Better Auth セッションの
 * `user.role` を認可判定や JWT 署名に渡す前に必ず通すこと。
 */
export function narrowRole(value: string): UserRole {
  return (ALLOWED_ROLES as readonly string[]).includes(value)
    ? (value as UserRole)
    : "user";
}
