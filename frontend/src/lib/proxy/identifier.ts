/**
 * client IP 抽出の純関数。
 *
 * proxy.ts から渡された header 値と環境フラグだけで IP を解決する。tier への
 * 写像 (rl:ip / rl:sess / rl:rsc / rl:uwrite) は rate-limit-plan.ts が担う。
 *
 * production は Fly.io edge proxy が上書きする Fly-Client-IP だけを信頼する。
 * 欠如時は fail-closed で null を返し、呼び出し側が経路異常として扱う。
 * dev/test では Fly-Client-IP → x-forwarded-for 第一値 → x-real-ip の順に
 * fallback する。
 */

/**
 * trusted / untrusted header 群と環境フラグから client IP を抽出する純関数。
 *
 * `Fly-Client-IP` は Fly.io edge proxy が必ず上書きで付与するため client から
 * 偽装不能。`x-forwarded-for` / `x-real-ip` は dev / test 用の fallback で、
 * production では信頼しない (詐称された値を per-IP rate limit に使わない)。
 */
export function extractClientIp(
  flyClientIp: string | null,
  forwardedFor: string | null,
  realIp: string | null,
  isProduction: boolean,
): string | null {
  if (flyClientIp) {
    const trimmed = flyClientIp.trim();
    if (trimmed) return trimmed;
  }
  // production では詐称可能な XFF/X-Real-IP を採用せず、
  // "unknown" bucket に集約する。
  if (isProduction) {
    return null;
  }
  if (forwardedFor) {
    const first = forwardedFor.split(",")[0]?.trim();
    if (first) return first;
  }
  if (realIp) {
    const trimmed = realIp.trim();
    if (trimmed) return trimmed;
  }
  return null;
}
