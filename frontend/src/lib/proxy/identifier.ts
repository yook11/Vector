/**
 * client IP 抽出の純関数。
 *
 * proxy.ts から渡された header 値と信頼モードだけで IP を解決する。tier への
 * 写像 (rl:ip / rl:sess / rl:rsc / rl:uwrite) は rate-limit-plan.ts が担う。
 *
 * production の信頼構造はプラットフォーム固有 (Fly は edge が上書きする
 * Fly-Client-IP、ALB は自身が XFF 末尾へ追記した値) のため、推測せず
 * CLIENT_IP_TRUST で明示宣言させる。未宣言は fail-closed (null) とし、
 * 呼び出し側が missing_ip 信号で可視化する。
 * 詳細は specs/client-ip-trust-mode.md。
 */

import { z } from "zod";

/** proxy が解決済み IP を下流 (route handler / Better Auth) へ渡す内部ヘッダ。 */
export const CLIENT_IP_HEADER = "x-vector-client-ip";

export type ClientIpTrust = "fly-client-ip" | "alb-xff-last";

/** CLIENT_IP_TRUST の宣言値を解釈する。有効値以外は null (fail-closed)。 */
export function parseClientIpTrust(
  raw: string | undefined | null,
): ClientIpTrust | null {
  if (raw === "fly-client-ip" || raw === "alb-xff-last") return raw;
  return null;
}

/**
 * IP literal のみ採用する。非 IP を通すと `rl:ip:<value>` は「解決済み」として
 * 数える一方、Better Auth は自前の検証で弾いて共有バケツへ落ちるため、消費者間で
 * identity 解釈が食い違う (missing_ip も出ない観測不能な劣化)。
 */
function asClientIp(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  const isIpLiteral =
    z.ipv4().safeParse(trimmed).success || z.ipv6().safeParse(trimmed).success;
  return isIpLiteral ? trimmed : null;
}

/**
 * 信頼モードと header 値から client IP を抽出する純関数。
 *
 * - `fly-client-ip`: Fly edge が必ず上書きする値のみ信頼する。
 * - `alb-xff-last`: ALB が実測接続元を追記した XFF **末尾**のみ信頼する。
 *   左側の値と `fly-client-ip` は client が自由に書けるため読まない。
 * - dev / test は信頼境界が無いため trust を無視し、手元ツール向けの
 *   fallback (fly-client-ip → XFF 先頭 → x-real-ip) を維持する。
 */
export function extractClientIp({
  trust,
  flyClientIp,
  forwardedFor,
  realIp,
  isProduction,
}: {
  trust: ClientIpTrust | null;
  flyClientIp: string | null;
  forwardedFor: string | null;
  realIp: string | null;
  isProduction: boolean;
}): string | null {
  if (isProduction) {
    if (trust === "fly-client-ip") {
      return asClientIp(flyClientIp);
    }
    if (trust === "alb-xff-last") {
      return asClientIp(forwardedFor?.split(",").at(-1));
    }
    return null;
  }
  return (
    asClientIp(flyClientIp) ??
    asClientIp(forwardedFor?.split(",")[0]) ??
    asClientIp(realIp)
  );
}
