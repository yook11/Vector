/**
 * リクエスト識別子抽出の純関数群。
 *
 * 副作用 (NextRequest からの header / cookie 読み出し) は呼び出し側 (proxy.ts) に
 * 残し、ここでは「与えられた値から識別子を組み立てる」だけを担当する。
 *
 * 識別子の優先順位:
 *   1. Better Auth session cookie が存在 → 認証済み user として cookie 値を hash
 *   2. cookie 無し → x-forwarded-for 第一値 → x-real-ip の順で client IP を採用
 *   3. どちらも無し → null (caller 側でフェイルオープン判断)
 */

import { createHash } from "node:crypto";

export type RequestIdentifier =
  | { kind: "auth"; key: string }
  | { kind: "anon"; key: string };

/**
 * cookie 値を SHA-256 して先頭 16 文字 (= 64 bit) を識別子にする。
 * 64 bit = 衝突確率 birthday bound で約 4.3 億 user で 1% — Vector の規模で実用上ゼロ。
 * raw cookie を Redis に書き込まないことで、Redis 漏洩時に session token が
 * そのまま流出する事故を構造的に防ぐ。
 */
export function hashSessionCookie(cookieValue: string): string {
  return createHash("sha256").update(cookieValue).digest("hex").slice(0, 16);
}

/**
 * x-forwarded-for は "client, proxy1, proxy2" の形式。第一値が真の client。
 * x-real-ip は nginx 等が単一 IP を入れる慣例。
 *
 * ⚠️ 信頼性は reverse proxy 設定に依存する。docker-compose 直接公開の現状では
 * client が任意に詐称可能 (per-IP throttle 回避)。Sprint 3 で reverse proxy /
 * Fly Edge 経由に切り替え、`Fly-Client-IP` 等 trusted header に差し替える。
 */
export function extractClientIp(
  forwardedFor: string | null,
  realIp: string | null,
): string | null {
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

export function buildIdentifier(
  sessionCookieValue: string | null,
  forwardedFor: string | null,
  realIp: string | null,
): RequestIdentifier | null {
  if (sessionCookieValue?.trim()) {
    return { kind: "auth", key: hashSessionCookie(sessionCookieValue) };
  }
  const ip = extractClientIp(forwardedFor, realIp);
  if (ip) {
    return { kind: "anon", key: ip };
  }
  return null;
}
