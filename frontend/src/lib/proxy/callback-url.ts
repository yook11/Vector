/**
 * Open redirect 対策の純関数。
 *
 * protocol-relative URL (`//evil.com`) や絶対 URL (`http://...`) を callbackUrl に
 * 埋め込ませないため、内部パスかどうかを構造的に判定する。
 */

export function isInternalPath(pathname: string): boolean {
  return pathname.startsWith("/") && !pathname.startsWith("//");
}

export function sanitizeCallbackUrl(pathname: string): string | null {
  return isInternalPath(pathname) ? pathname : null;
}
