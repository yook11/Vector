/**
 * リクエスト識別子抽出の純関数群。
 *
 * 副作用 (NextRequest からの header 読み出し) は呼び出し側 (proxy.ts) に残し、
 * ここでは「与えられたヘッダ値から識別子を組み立てる」だけを担当する。
 *
 * 識別子は IP-based に統一 (red-team C1 / F2-F4 対策):
 *   - cookie 値を identifier に使うと「任意の non-empty cookie で別 bucket」
 *     「auth bucket への昇格 (limit 2x)」両方の bypass 経路が開く。proxy 層の
 *     rate-limit は per-IP の上限を確実に課す責務に絞り、認証状態に応じた
 *     limit 緩和は後段 (Better Auth 内蔵 rate-limit / backend 側) に任せる。
 *
 * 識別子の優先順位:
 *   1. x-forwarded-for 第一値
 *   2. x-real-ip
 *   3. どちらも無し → "unknown" bucket (curl --no-headers 等の非正規 request を
 *      1 つの bucket に集約する fail-closed fallback)。攻撃者が "unknown" を
 *      消費しても他の "unknown" 群が共倒れするだけで IP-bucket 群への影響なし。
 */

export type RequestIdentifier = { kind: "ip"; key: string };

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
  forwardedFor: string | null,
  realIp: string | null,
): RequestIdentifier {
  const ip = extractClientIp(forwardedFor, realIp);
  return { kind: "ip", key: ip ?? "unknown" };
}
