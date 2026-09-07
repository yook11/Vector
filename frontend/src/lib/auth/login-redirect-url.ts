import {
  hasUnsafeUrlCharacters,
  parseLoginCallback,
} from "@/lib/auth/login-callback";

const LOGIN_FALLBACK = "/auth/login" as const;

export function buildLoginCallbackUrl(referer: string | null): string {
  if (!referer || referer.trim() !== referer || hasUnsafeUrlCharacters(referer))
    return LOGIN_FALLBACK;
  try {
    const url = new URL(referer);
    if (url.protocol !== "https:" && url.protocol !== "http:")
      return LOGIN_FALLBACK;
    // originは復帰先に採用せず、正規化で消える前のパスも共通スキーマで検証する。
    const match = /^https?:\/\/[^/?#]+(.*)$/i.exec(referer);
    if (!match) return LOGIN_FALLBACK;
    const tail = match[1] ?? "";
    const callback = parseLoginCallback(
      tail.startsWith("/") ? tail : `/${tail}`,
    );
    return callback
      ? `${LOGIN_FALLBACK}?callbackUrl=${encodeURIComponent(callback)}`
      : LOGIN_FALLBACK;
  } catch {
    return LOGIN_FALLBACK;
  }
}
