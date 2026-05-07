/**
 * リクエスト識別子抽出の純関数群。
 *
 * 副作用 (NextRequest からの header 読み出しと環境判定) は呼び出し側 (proxy.ts)
 * に残し、ここでは「与えられたヘッダ値と環境フラグから識別子を組み立てる」だけを
 * 担当する純関数として実装する。
 *
 * 識別子は IP-based に統一 (red-team C1 / F2-F4 対策):
 *   - cookie 値を identifier に使うと「任意の non-empty cookie で別 bucket」
 *     「auth bucket への昇格 (limit 2x)」両方の bypass 経路が開く。proxy 層の
 *     rate-limit は per-IP の上限を確実に課す責務に絞り、認証状態に応じた
 *     limit 緩和は後段 (Better Auth 内蔵 rate-limit / backend 側) に任せる。
 *
 * 信頼境界 (red-team C1 / F2-F4 構造防御 / PR10 で確立):
 *   - production runtime は Fly.io edge proxy 経由で必ず `Fly-Client-IP` を
 *     上書き付与される (incoming 値を信頼せず、TCP 接続元の真の client IP に
 *     差し替える)。client から偽装不能。
 *   - production で Fly-Client-IP 欠如 = Fly proxy bypass された異常経路。
 *     詐称された `x-forwarded-for` を採用すると per-IP rate limit が回避される
 *     ため、fail-closed で `null` を返し "unknown" bucket に集約する
 *     (memory feedback_structural_guarantee.md / feedback_failure_visibility.md)。
 *   - development / test (docker-compose / npm run dev) は Fly Edge を経由
 *     しないため、`x-forwarded-for` 第一値 → `x-real-ip` の従来 fallback を維持。
 *
 * 識別子の優先順位 (production):
 *   1. `Fly-Client-IP` (Fly.io edge 注入、trusted)
 *   2. fail-closed → `null` → 呼び出し側で "unknown" bucket
 *
 * 識別子の優先順位 (development / test):
 *   1. `Fly-Client-IP` (将来 Fly Edge 経由 dev に切り替えた場合に備える)
 *   2. `x-forwarded-for` 第一値
 *   3. `x-real-ip`
 *   4. `null` → 呼び出し側で "unknown" bucket (curl --no-headers 等の非正規
 *      request を 1 つの bucket に集約する fail-closed fallback)。攻撃者が
 *      "unknown" を消費しても他の "unknown" 群が共倒れするだけで IP-bucket
 *      群への影響なし。
 */

export type RequestIdentifier = { kind: "ip"; key: string };

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
  // production で Fly-Client-IP が無い = Fly proxy bypass / Fly Edge 設定崩壊。
  // 詐称可能な x-forwarded-for を信頼すると per-IP rate limit が回避される
  // ため、ここで打ち切って "unknown" bucket に全異常 request を集約する。
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

export function buildIdentifier(
  flyClientIp: string | null,
  forwardedFor: string | null,
  realIp: string | null,
  isProduction: boolean,
): RequestIdentifier {
  const ip = extractClientIp(flyClientIp, forwardedFor, realIp, isProduction);
  return { kind: "ip", key: ip ?? "unknown" };
}
