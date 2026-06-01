/**
 * Rate limit の tier plan を組む純関数。
 *
 * request を **class (rsc / read / mutation) × identity (session / IP)** で分類し、
 * 該当する全 tier (key + limit) を列挙する。実際の Redis 判定は rate-limit.ts が
 * 「全 tier を満たせば allow / 1 つでも超過で block」として atomic に行う。
 *
 * 識別境界:
 * - `_rsc` GET は prefetch fan-out 由来なので寛容な ceiling (`rl:rsc:<ip>`) を別財布で持つ。
 *   全 skip は C8 (認証済 RSC フラッド→pool 枯渇) を再オープンするため count は維持する。
 * - 認証済 request は `rl:sess:<sha256(token)[:16]>` で sub-bucket を作るが、署名検証なしの
 *   session cookie を単独キーにすると偽造で無限バケット化するため、IP が解決できる限り
 *   `rl:ip:<ip>` ceiling を必ず併置する (two-tier-AND の forge-bypass backstop)。
 * - IP 未解決 (production で Fly-Client-IP 欠如) は identity でなく経路異常として扱う。
 *   read/`_rsc` は fail-open、anon mutation のみ最小限 `rl:uwrite:global` で縛る。
 *
 * 純度: I/O・時刻・乱数を持たない。`Date.now()` は eval 直前に rate-limit.ts が生成する。
 */

import { extractClientIp } from "@/lib/proxy/identifier";

export type RateLimitTier = { key: string; limit: number };

/**
 * 観測信号。production で IP が未解決 (経路異常) のときだけ立てる。
 * - missing_ip: IP 未解決だが count は継続 / fail-open した read・`_rsc`・session 単独。
 * - unknown_write: IP 未解決 & session 無の anon mutation (共有 global bucket で縛った)。
 */
export type RateLimitSignal = "missing_ip" | "unknown_write";

/**
 * tier の列挙。`tiers` が空なら構造的に fail-open (eval を呼ばない)。
 * `failOpen` フラグを別に持たないのは、空配列がそのまま「縛らない」を表すため。
 */
export type RateLimitPlan = {
  tiers: RateLimitTier[];
  signal?: RateLimitSignal;
};

/** 各 tier の上限値 (req/min)。rate-limit.ts が env から組んで渡す。 */
export type RateLimitLimits = {
  rsc: number;
  session: number;
  ip: number;
  unknownWrite: number;
};

import { createHash } from "node:crypto";

function sessionKey(token: string): string {
  const digest = createHash("sha256").update(token).digest("hex").slice(0, 16);
  return `rl:sess:${digest}`;
}

function isReadMethod(method: string): boolean {
  return method === "GET" || method === "HEAD" || method === "OPTIONS";
}

export type BuildRateLimitPlanArgs = {
  method: string;
  hasRsc: boolean;
  flyClientIp: string | null;
  forwardedFor: string | null;
  realIp: string | null;
  sessionToken: string | null;
  isProduction: boolean;
  limits: RateLimitLimits;
};

export function buildRateLimitPlan({
  method,
  hasRsc,
  flyClientIp,
  forwardedFor,
  realIp,
  sessionToken,
  isProduction,
  limits,
}: BuildRateLimitPlanArgs): RateLimitPlan {
  const ip = extractClientIp(flyClientIp, forwardedFor, realIp, isProduction);
  // production で IP 未解決のときだけ観測信号を出す。dev は Fly Edge 非経由で
  // IP 未解決が常態のため信号を出さない。
  const missingIpSignal: RateLimitSignal | undefined =
    isProduction && ip === null ? "missing_ip" : undefined;

  // `_rsc` GET: prefetch fan-out 用の寛容 ceiling。IP 未解決は fail-open。
  if (hasRsc && method === "GET") {
    if (ip === null) {
      return { tiers: [], signal: missingIpSignal };
    }
    return { tiers: [{ key: `rl:rsc:${ip}`, limit: limits.rsc }] };
  }

  // read / mutation: session sub-bucket + IP ceiling の two-tier-AND。
  const tiers: RateLimitTier[] = [];
  if (sessionToken) {
    tiers.push({ key: sessionKey(sessionToken), limit: limits.session });
  }
  if (ip !== null) {
    tiers.push({ key: `rl:ip:${ip}`, limit: limits.ip });
  }

  if (tiers.length > 0) {
    return { tiers, signal: missingIpSignal };
  }

  // session 無 & IP 未解決の終端。read は fail-open、mutation のみ最小限縛る。
  if (isReadMethod(method)) {
    return { tiers: [], signal: missingIpSignal };
  }
  return {
    tiers: [{ key: "rl:uwrite:global", limit: limits.unknownWrite }],
    signal: isProduction ? "unknown_write" : undefined,
  };
}
