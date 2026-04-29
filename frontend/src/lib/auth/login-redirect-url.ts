/**
 * Server Action から login redirect 先 URL を組み立てる純関数。
 *
 * Server Action は browser からの fetch なので Referer header が submit 元 page
 * の URL になる。これを callbackUrl として埋め込み、ログイン後に元の page へ戻す。
 *
 * Open redirect 対策の構造:
 *   - same-origin 以外は捨てる (URL parser に通して例外なら捨てる)
 *   - protocol-relative (`//evil.com`) は捨てる
 *   - `/auth/*` 自体への redirect は callbackUrl 無し (再帰防止)
 *
 * 副作用 (`headers()` 取得) は呼出側 (guards.ts) に残し、ここでは referer 文字列を
 * 引数で受けて URL 文字列を返すだけにする。
 */

const LOGIN_FALLBACK = "/auth/login" as const;

export function buildLoginCallbackUrl(referer: string | null): string {
  if (!referer) return LOGIN_FALLBACK;

  let pathname: string;
  let search: string;
  try {
    const url = new URL(referer);
    pathname = url.pathname;
    search = url.search;
  } catch {
    return LOGIN_FALLBACK;
  }

  if (!pathname.startsWith("/") || pathname.startsWith("//")) {
    return LOGIN_FALLBACK;
  }
  if (pathname.startsWith("/auth")) {
    return LOGIN_FALLBACK;
  }
  return `${LOGIN_FALLBACK}?callbackUrl=${encodeURIComponent(pathname + search)}`;
}
