import { describe, expect, it } from "vitest";
import {
  buildRateLimitPlan,
  buildSseRateLimitPlan,
  isHealthCheckerUserAgent,
  type RateLimitLimits,
} from "./rate-limit-plan";

// 固定 limits で tier 表の全セルを oracle 化する。
const LIMITS: RateLimitLimits = {
  rsc: 600,
  session: 60,
  ip: 300,
  unknownWrite: 30,
};

// sha256(token)[:16] の既知ベクタ (node:crypto で算出)。
const KNOWN_TOKEN = "known-session-token";
const KNOWN_SESS_KEY = "rl:sess:55d0802966c289e9";

type Args = Parameters<typeof buildRateLimitPlan>[0];

// clientIp は呼び出し側 (proxy.ts) が identifier.ts で解決済みの値を渡す前提。
// このモジュールはヘッダ由来の解決順を持たないため、解決済み文字列をそのまま渡す。
function build(overrides: Partial<Args> = {}) {
  return buildRateLimitPlan({
    method: "GET",
    hasRsc: false,
    clientIp: null,
    sessionToken: null,
    isProduction: true,
    limits: LIMITS,
    ...overrides,
  });
}

const READ_METHODS = ["GET", "HEAD", "OPTIONS"] as const;
const MUTATION_METHODS = ["POST", "PUT", "PATCH", "DELETE"] as const;

describe("buildSseRateLimitPlan — dedicated connection-start tiers", () => {
  it("applies session+run, session, and IP tiers together", () => {
    const plan = buildSseRateLimitPlan({
      sessionIdentity: "unverified-session-token",
      runId: "00000000-0000-4000-a000-000000000010",
      clientIp: "203.0.113.5",
      isProduction: true,
    });

    expect(plan.tiers).toHaveLength(3);
    expect(plan.tiers[0]).toMatchObject({ limit: 12 });
    expect(plan.tiers[0]?.key).toMatch(
      /^rl:sse:session-run:[0-9a-f]{16}:00000000-0000-4000-a000-000000000010$/,
    );
    expect(plan.tiers[1]).toMatchObject({ limit: 30 });
    expect(plan.tiers[1]?.key).toMatch(/^rl:sse:session:[0-9a-f]{16}$/);
    expect(plan.tiers[2]).toEqual({
      key: "rl:sse:ip:203.0.113.5",
      limit: 120,
    });
  });

  it("keeps the two session tiers and signals when production IP is missing", () => {
    const plan = buildSseRateLimitPlan({
      sessionIdentity: "unverified-session-token",
      runId: "00000000-0000-4000-a000-000000000010",
      clientIp: null,
      isProduction: true,
    });

    expect(plan.tiers).toHaveLength(2);
    expect(plan.tiers.some((tier) => tier.key.startsWith("rl:sse:ip:"))).toBe(
      false,
    );
    expect(plan.signal).toBe("missing_ip");
  });

  it("uses independent limits supplied by configuration", () => {
    const plan = buildSseRateLimitPlan({
      sessionIdentity: "unverified-session-token",
      runId: "00000000-0000-4000-a000-000000000010",
      clientIp: "203.0.113.5",
      isProduction: true,
      limits: { sessionRun: 2, session: 3, ip: 4 },
    });

    expect(plan.tiers.map((tier) => tier.limit)).toEqual([2, 3, 4]);
  });

  it("limits an unauthenticated request by IP before session lookup", () => {
    const plan = buildSseRateLimitPlan({
      sessionIdentity: null,
      runId: "00000000-0000-4000-a000-000000000010",
      clientIp: "203.0.113.5",
      isProduction: true,
    });

    expect(plan.tiers).toEqual([{ key: "rl:sse:ip:203.0.113.5", limit: 120 }]);
  });
});

describe("buildRateLimitPlan — _rsc GET tier (寛容 ceiling / 別財布)", () => {
  it("IP 解決時は rl:rsc:<ip> 600 のみ (session 無)", () => {
    const plan = build({
      method: "GET",
      hasRsc: true,
      clientIp: "203.0.113.5",
    });
    expect(plan.tiers).toEqual([{ key: "rl:rsc:203.0.113.5", limit: 600 }]);
    expect(plan.signal).toBeUndefined();
  });

  it("IP 解決時は session があっても rl:rsc:<ip> 600 のみ (sess/ip tier を足さない)", () => {
    const plan = build({
      method: "GET",
      hasRsc: true,
      clientIp: "203.0.113.5",
      sessionToken: KNOWN_TOKEN,
    });
    expect(plan.tiers).toEqual([{ key: "rl:rsc:203.0.113.5", limit: 600 }]);
  });

  it("IP 未解決 (prod) は fail-open (tiers 空) + missing_ip signal", () => {
    const plan = build({ method: "GET", hasRsc: true, clientIp: null });
    expect(plan.tiers).toEqual([]);
    expect(plan.signal).toBe("missing_ip");
  });

  it("_rsc は GET 厳密一致のみ — POST ?_rsc=1 は rsc tier(600) を使わず mutation 経路", () => {
    const plan = build({
      method: "POST",
      hasRsc: true,
      clientIp: "203.0.113.5",
    });
    expect(plan.tiers).toEqual([{ key: "rl:ip:203.0.113.5", limit: 300 }]);
    expect(plan.tiers.some((t) => t.key.startsWith("rl:rsc:"))).toBe(false);
    expect(plan.tiers.some((t) => t.limit === 600)).toBe(false);
  });
});

describe("buildRateLimitPlan — read (GET/HEAD/OPTIONS) tier", () => {
  it("IP 解決 / session 有 → rl:sess 60 + rl:ip 300 (two-tier-AND)", () => {
    const plan = build({
      method: "GET",
      clientIp: "203.0.113.5",
      sessionToken: KNOWN_TOKEN,
    });
    expect(plan.tiers).toEqual([
      { key: KNOWN_SESS_KEY, limit: 60 },
      { key: "rl:ip:203.0.113.5", limit: 300 },
    ]);
    expect(plan.signal).toBeUndefined();
  });

  it("IP 解決 / session 無 → rl:ip 300 のみ", () => {
    const plan = build({ method: "GET", clientIp: "203.0.113.5" });
    expect(plan.tiers).toEqual([{ key: "rl:ip:203.0.113.5", limit: 300 }]);
  });

  it("IP 未解決 / session 有 (prod) → rl:sess 60 のみ + missing_ip", () => {
    const plan = build({
      method: "GET",
      clientIp: null,
      sessionToken: "token-a",
    });
    expect(plan.tiers).toEqual([
      { key: "rl:sess:a70bf50e531ce1a8", limit: 60 },
    ]);
    expect(plan.signal).toBe("missing_ip");
  });

  it("IP 未解決 / session 無 (prod) → fail-open (tiers 空) + missing_ip", () => {
    const plan = build({ method: "GET", clientIp: null });
    expect(plan.tiers).toEqual([]);
    expect(plan.signal).toBe("missing_ip");
  });

  it("HEAD は read 経路 — IP 未解決 & session 無で tiers 空 (uwrite:global を使わない)", () => {
    const plan = build({ method: "HEAD", clientIp: null });
    expect(plan.tiers).toEqual([]);
    expect(plan.tiers.some((t) => t.key === "rl:uwrite:global")).toBe(false);
  });

  it("OPTIONS は read 経路 — IP 未解決 & session 無で tiers 空 (CORS preflight が global を消費しない)", () => {
    const plan = build({ method: "OPTIONS", clientIp: null });
    expect(plan.tiers).toEqual([]);
    expect(plan.tiers.some((t) => t.key === "rl:uwrite:global")).toBe(false);
  });
});

describe("buildRateLimitPlan — mutation (POST/PUT/PATCH/DELETE) tier", () => {
  it("IP 解決 / session 有 → rl:sess 60 + rl:ip 300", () => {
    const plan = build({
      method: "POST",
      clientIp: "203.0.113.5",
      sessionToken: "token-a",
    });
    expect(plan.tiers).toEqual([
      { key: "rl:sess:a70bf50e531ce1a8", limit: 60 },
      { key: "rl:ip:203.0.113.5", limit: 300 },
    ]);
  });

  it("IP 解決 / session 無 → rl:ip 300 のみ", () => {
    const plan = build({ method: "POST", clientIp: "203.0.113.5" });
    expect(plan.tiers).toEqual([{ key: "rl:ip:203.0.113.5", limit: 300 }]);
  });

  it("IP 未解決 / session 有 (prod) → rl:sess 60 のみ + missing_ip", () => {
    const plan = build({
      method: "POST",
      clientIp: null,
      sessionToken: "token-a",
    });
    expect(plan.tiers).toEqual([
      { key: "rl:sess:a70bf50e531ce1a8", limit: 60 },
    ]);
    expect(plan.signal).toBe("missing_ip");
  });

  it("IP 未解決 / session 無 (prod) → rl:uwrite:global 30 + unknown_write", () => {
    const plan = build({ method: "POST", clientIp: null });
    expect(plan.tiers).toEqual([{ key: "rl:uwrite:global", limit: 30 }]);
    expect(plan.signal).toBe("unknown_write");
  });

  it.each(
    MUTATION_METHODS,
  )("%s は IP 未解決 & session 無で rl:uwrite:global を使う (mutation 経路)", (method) => {
    const plan = build({ method, clientIp: null });
    expect(plan.tiers).toEqual([{ key: "rl:uwrite:global", limit: 30 }]);
    expect(plan.signal).toBe("unknown_write");
  });
});

describe("buildRateLimitPlan — forge-bypass 不変 (IP ceiling backstop)", () => {
  it.each([
    ...READ_METHODS,
    ...MUTATION_METHODS,
  ])("%s: IP 解決 & 非_rsc は session 無で必ず rl:ip tier を含む", (method) => {
    const plan = build({ method, clientIp: "203.0.113.5" });
    expect(plan.tiers.some((t) => t.key === "rl:ip:203.0.113.5")).toBe(true);
  });

  it.each([
    ...READ_METHODS,
    ...MUTATION_METHODS,
  ])("%s: IP 解決 & 非_rsc は session 有でも必ず rl:ip tier を含む (session 単独にしない)", (method) => {
    const plan = build({
      method,
      clientIp: "203.0.113.5",
      sessionToken: KNOWN_TOKEN,
    });
    expect(plan.tiers.some((t) => t.key === "rl:ip:203.0.113.5")).toBe(true);
  });

  it("session 単独 tier は IP 未解決時のみ成立する", () => {
    const resolved = build({
      method: "GET",
      clientIp: "203.0.113.5",
      sessionToken: KNOWN_TOKEN,
    });
    // IP 解決時は sess 単独にならない (ip tier が必ず付く)
    expect(resolved.tiers).toHaveLength(2);
    const unresolved = build({
      method: "GET",
      clientIp: null,
      sessionToken: KNOWN_TOKEN,
    });
    expect(unresolved.tiers).toEqual([{ key: KNOWN_SESS_KEY, limit: 60 }]);
  });
});

describe("buildRateLimitPlan — session key (sha256 hash)", () => {
  it("生成 key が raw token を含まない", () => {
    const plan = build({
      method: "GET",
      clientIp: "203.0.113.5",
      sessionToken: KNOWN_TOKEN,
    });
    const sessTier = plan.tiers.find((t) => t.key.startsWith("rl:sess:"));
    expect(sessTier?.key).not.toContain(KNOWN_TOKEN);
  });

  it("rl:sess:<16 hex> 形式", () => {
    const plan = build({
      method: "GET",
      clientIp: null,
      sessionToken: KNOWN_TOKEN,
    });
    expect(plan.tiers[0]?.key).toMatch(/^rl:sess:[0-9a-f]{16}$/);
  });

  it("既知ベクタの期待値に一致する", () => {
    const plan = build({
      method: "GET",
      clientIp: null,
      sessionToken: KNOWN_TOKEN,
    });
    expect(plan.tiers[0]?.key).toBe(KNOWN_SESS_KEY);
  });

  it("同 token → 同 key、異 token → 異 key", () => {
    const a1 = build({ clientIp: null, sessionToken: "token-a" }).tiers[0]?.key;
    const a2 = build({ clientIp: null, sessionToken: "token-a" }).tiers[0]?.key;
    const b = build({ clientIp: null, sessionToken: "token-b" }).tiers[0]?.key;
    expect(a1).toBe(a2);
    expect(a1).not.toBe(b);
  });

  it("空文字 session は session 無扱い (バイパス防止)", () => {
    const plan = build({
      method: "GET",
      clientIp: "203.0.113.5",
      sessionToken: "",
    });
    expect(plan.tiers).toEqual([{ key: "rl:ip:203.0.113.5", limit: 300 }]);
    expect(plan.tiers.some((t) => t.key.startsWith("rl:sess:"))).toBe(false);
  });
});

describe("buildRateLimitPlan — signal は production のみ", () => {
  it("prod / read IP 未解決 / session 無 → missing_ip", () => {
    expect(build({ method: "GET", clientIp: null }).signal).toBe("missing_ip");
  });

  it("prod / _rsc IP 未解決 → missing_ip", () => {
    expect(build({ method: "GET", hasRsc: true, clientIp: null }).signal).toBe(
      "missing_ip",
    );
  });

  it("prod / session 単独 (IP 未解決) → missing_ip", () => {
    expect(
      build({ method: "GET", clientIp: null, sessionToken: "token-a" }).signal,
    ).toBe("missing_ip");
  });

  it("prod / mutation 終端 → unknown_write", () => {
    expect(build({ method: "POST", clientIp: null }).signal).toBe(
      "unknown_write",
    );
  });

  it("dev / read IP 未解決 / session 無 → signal undefined", () => {
    expect(
      build({ method: "GET", clientIp: null, isProduction: false }).signal,
    ).toBeUndefined();
  });

  it("dev / mutation 終端 → tier は付くが signal undefined", () => {
    const plan = build({
      method: "POST",
      clientIp: null,
      isProduction: false,
    });
    expect(plan.tiers).toEqual([{ key: "rl:uwrite:global", limit: 30 }]);
    expect(plan.signal).toBeUndefined();
  });

  it("IP 解決時は signal を立てない", () => {
    expect(
      build({ method: "GET", clientIp: "203.0.113.5" }).signal,
    ).toBeUndefined();
  });
});

describe("buildRateLimitPlan — limits override の貫通", () => {
  it("渡した limits 値が各 tier に反映される", () => {
    const custom: RateLimitLimits = {
      rsc: 999,
      session: 11,
      ip: 22,
      unknownWrite: 7,
    };
    const rsc = buildRateLimitPlan({
      method: "GET",
      hasRsc: true,
      clientIp: "203.0.113.5",
      sessionToken: null,
      isProduction: true,
      limits: custom,
    });
    expect(rsc.tiers).toEqual([{ key: "rl:rsc:203.0.113.5", limit: 999 }]);

    const both = buildRateLimitPlan({
      method: "POST",
      hasRsc: false,
      clientIp: "203.0.113.5",
      sessionToken: "token-a",
      isProduction: true,
      limits: custom,
    });
    expect(both.tiers).toEqual([
      { key: "rl:sess:a70bf50e531ce1a8", limit: 11 },
      { key: "rl:ip:203.0.113.5", limit: 22 },
    ]);

    const uwrite = buildRateLimitPlan({
      method: "POST",
      hasRsc: false,
      clientIp: null,
      sessionToken: null,
      isProduction: true,
      limits: custom,
    });
    expect(uwrite.tiers).toEqual([{ key: "rl:uwrite:global", limit: 7 }]);
  });
});

describe("isHealthCheckerUserAgent", () => {
  it("ELB-HealthChecker/ prefix を health checker と判定する", () => {
    expect(isHealthCheckerUserAgent("ELB-HealthChecker/2.0")).toBe(true);
  });

  it("prefix 一致のみを true とする (末尾一致・部分一致は含まない)", () => {
    expect(isHealthCheckerUserAgent("Mozilla/5.0 ELB-HealthChecker/2.0")).toBe(
      false,
    );
  });

  it("通常の browser user-agent は false", () => {
    expect(
      isHealthCheckerUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)"),
    ).toBe(false);
  });

  it("null は false", () => {
    expect(isHealthCheckerUserAgent(null)).toBe(false);
  });

  it("空文字は false", () => {
    expect(isHealthCheckerUserAgent("")).toBe(false);
  });
});
