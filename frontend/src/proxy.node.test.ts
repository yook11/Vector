import { NextRequest } from "next/server";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  type MockInstance,
  vi,
} from "vitest";

vi.mock("server-only", () => ({}));

// redis mock — module load 前に hoist
const mockEval = vi.fn();
const mockConnect = vi.fn();
const mockOn = vi.fn();
let mockIsOpenValue = false;

vi.mock("redis", () => ({
  createClient: vi.fn(() => ({
    on: mockOn,
    connect: mockConnect,
    eval: mockEval,
    get isOpen() {
      return mockIsOpenValue;
    },
  })),
}));

import { proxy } from "./proxy";

const g = globalThis as unknown as {
  __vectorRateLimitRedis?: unknown;
  __vectorRateLimitErrorLastMs?: number;
  __vectorRateLimitSignalLastMs?: Record<string, number>;
  __vectorRateLimitFailOpenLastMs?: Record<string, number>;
  // CLIENT_IP_TRUST 未宣言 warn の once-flag。既存の
  // __vectorRateLimitMisconfigLogged と同じ globalThis-once 規約を踏襲する想定。
  __vectorClientIpTrustUnconfiguredLogged?: boolean;
  // XFF chain 観測のウィンドウ集計 (frontend_xff_chain_observed)。他 request 由来の
  // 蓄積が漏れないよう、他の singleton state と同様に test ごとに reset する。
  __vectorXffChainWindow?: unknown;
};

let warnSpy: MockInstance<typeof console.warn>;

beforeEach(() => {
  delete g.__vectorRateLimitRedis;
  delete g.__vectorRateLimitErrorLastMs;
  delete g.__vectorRateLimitSignalLastMs;
  delete g.__vectorRateLimitFailOpenLastMs;
  delete g.__vectorClientIpTrustUnconfiguredLogged;
  delete g.__vectorXffChainWindow;
  mockEval.mockReset();
  mockConnect.mockReset();
  mockOn.mockReset();
  mockIsOpenValue = false;
  vi.stubEnv("REDIS_URL_RL", "redis://test-rl:6379/0");
  vi.stubEnv("BETTER_AUTH_URL", "http://localhost:3000");
  vi.stubEnv("NODE_ENV", "test");
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  warnSpy.mockRestore();
  vi.useRealTimers();
});

function mockNextRequest(
  url: string,
  init: { headers?: Record<string, string>; method?: string } = {},
): NextRequest {
  const headers = new Headers(init.headers ?? {});
  return new NextRequest(url, { method: init.method ?? "GET", headers });
}

/** warnSpy が捕捉した logServerEvent JSON のうち、最初に一致する event を返す。 */
function findLoggedEvent(event: string): Record<string, unknown> | undefined {
  for (const call of warnSpy.mock.calls) {
    const raw = call[0];
    if (typeof raw !== "string") continue;
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (parsed.event === event) return parsed;
    } catch {
      // logRedisError は JSON でない warn を出すので無視。
    }
  }
  return undefined;
}

/** warnSpy が捕捉した logServerEvent JSON のうち、一致する event 全件を返す (once 回数検証用)。 */
function findLoggedEvents(event: string): Record<string, unknown>[] {
  const matches: Record<string, unknown>[] = [];
  for (const call of warnSpy.mock.calls) {
    const raw = call[0];
    if (typeof raw !== "string") continue;
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (parsed.event === event) matches.push(parsed);
    } catch {
      // logRedisError は JSON でない warn を出すので無視。
    }
  }
  return matches;
}

describe("proxy — rate-limit tier 結線 (ADR-009)", () => {
  it("SSE route is excluded from the ordinary read bucket", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest(
      "http://localhost:3000/api/research/runs/00000000-0000-4000-a000-000000000010/events",
      { headers: { "x-forwarded-for": "1.2.3.4" } },
    );

    await proxy(req);

    expect(mockEval).not.toHaveBeenCalled();
  });
  it("(1) anon POST /api/auth/sign-in/email (dev xff) は rl:ip:<ip> 300 で count される", () => {
    // /api/auth/* も rate-limit を経由するが handler には透過する。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest(
      "http://localhost:3000/api/auth/sign-in/email",
      {
        method: "POST",
        headers: { "x-forwarded-for": "1.2.3.4" },
      },
    );
    return proxy(req).then(() => {
      expect(mockEval).toHaveBeenCalledTimes(1);
      const args = mockEval.mock.calls[0]?.[1] as {
        keys: string[];
        arguments: string[];
      };
      expect(args.keys).toEqual(["rl:ip:1.2.3.4"]);
      expect(args.arguments[3]).toBe("300");
    });
  });

  it("(2) cookie/XFF/X-Real-IP すべて欠如の anon GET (dev) は fail-open で eval を呼ばない", async () => {
    // IP 未解決 & session 無の read は構造的 fail-open (tiers 空 → eval せず allow)。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
  });

  it("(2b) 同条件を production で踏むと fail-open + missing_ip signal を出す", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });

  it("(3) 上限超過 (eval=0) は 429 + Retry-After を返す", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(0); // Lua script が denied を返す
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    const res = await proxy(req);
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("60");
  });

  it("(4) cookie present + IP 未解決 は rl:sess:<hash> 単独で count し、cookie 生値を key に入れない", async () => {
    // IP 未解決時の session 単独 tier は ADR-009 で正 (cookie 値は hash 化)。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { cookie: "better-auth.session_token=AAAA" },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys).toHaveLength(1);
    expect(args.keys[0]).toMatch(/^rl:sess:[0-9a-f]{16}$/);
    expect(args.keys[0]).not.toContain("AAAA");
  });

  it("(5) Redis 障害時は fail-open で透過し warn を 1 度出す", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    const res = await proxy(req);
    expect(res.status).not.toBe(429);
    expect(warnSpy).toHaveBeenCalledOnce();
  });
});

function researchActionRequest(
  pathname: string,
  nextAction: string,
): NextRequest {
  return mockNextRequest(`http://localhost:3000${pathname}`, {
    method: "POST",
    headers: {
      cookie: "better-auth.session_token=AAAA",
      "x-forwarded-for": "1.2.3.4",
      "Next-Action": nextAction,
    },
  });
}

describe("proxy — research Server Actionは既存mutation rate limitを共有する", () => {
  it("submit・cancel・別mutationはいずれもsession 60とIP 300の同一tierで評価する", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(researchActionRequest("/research", "action-submit"));
    await proxy(
      researchActionRequest(
        "/research/00000000-0000-4000-a000-000000000001",
        "action-cancel",
      ),
    );
    await proxy(researchActionRequest("/watchlist", "action-watchlist"));

    expect(mockEval).toHaveBeenCalledTimes(3);
    const calls = mockEval.mock.calls.map(
      ([, args]) => args as { keys: string[]; arguments: string[] },
    );
    const expectedKeys = calls[0]?.keys;
    expect(expectedKeys).toEqual([
      expect.stringMatching(/^rl:sess:[0-9a-f]{16}$/),
      "rl:ip:1.2.3.4",
    ]);
    for (const call of calls) {
      expect(call.keys).toEqual(expectedKeys);
      expect(call.arguments.slice(3, 5)).toEqual(["60", "300"]);
      expect(call.arguments.slice(3, 5)).not.toContain("10");
    }
  });

  it("mutationがdenyされたらquota情報なしの終端429をCSP付与前に返す", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(0);

    const response = await proxy(
      researchActionRequest("/research", "action-submit-denied"),
    );

    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe("60");
    expect(response.headers.get("Content-Security-Policy")).toBeNull();
    expect(response.headers.get("x-middleware-next")).toBeNull();
    const body = await response.text();
    expect(body).toBe("Too Many Requests");
    expect(body).not.toContain("research_daily_request_limit_exceeded");
  });

  it("mutationのRedis eval障害はraw errorを記録せずfail-openする", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis password leaked"));

    const response = await proxy(
      researchActionRequest("/research", "action-submit-fail-open"),
    );

    expect(response.status).not.toBe(429);
    expect(response.headers.get("x-middleware-next")).toBe("1");
    const event = findLoggedEvent("frontend_rate_limit_redis_fail_open");
    expect(event).toStrictEqual({
      event: "frontend_rate_limit_redis_fail_open",
      level: "warn",
      requestClass: "mutation",
      errorType: "eval",
    });
    expect(JSON.stringify(event)).not.toContain("redis password leaked");
  });
});

describe("proxy — _rsc prefetch tier", () => {
  it("_rsc GET (fly 解決) は rl:rsc:<ip> 600 の寛容 ceiling で count する", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news?_rsc=abc123", {
      headers: { "fly-client-ip": "203.0.113.5" },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as {
      keys: string[];
      arguments: string[];
    };
    expect(args.keys).toEqual(["rl:rsc:203.0.113.5"]);
    expect(args.arguments[3]).toBe("600");
  });

  it("_rsc GET + 全 IP 欠如 (production) は fail-open (eval 呼ばない) + missing_ip signal", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news?_rsc=abc123");
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });
});

describe("proxy — anon mutation 終端 (IP 未解決)", () => {
  it("anon mutation + 全 IP 欠如 (production) は rl:uwrite:global 30 + unknown_write signal", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/api/some-mutation", {
      method: "POST",
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as {
      keys: string[];
      arguments: string[];
    };
    expect(args.keys).toEqual(["rl:uwrite:global"]);
    expect(args.arguments[3]).toBe("30");
    expect(findLoggedEvent("frontend_rate_limit_unknown_write")).toBeDefined();
  });
});

describe("proxy — identity 解決の dev/prod 分岐", () => {
  it("dev は fly 欠如時に xff 第一値を rl:ip key に使う", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys).toEqual(["rl:ip:1.2.3.4"]);
  });

  it("production は CLIENT_IP_TRUST 未設定だと xff を信頼せず fail-closed で anon read は fail-open する", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });
});

describe("proxy — CLIENT_IP_TRUST 経由の IP 解決 (production)", () => {
  it("trust=fly-client-ip は fly-client-ip を信頼して rl:ip tier を組む", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "fly-client-ip");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "fly-client-ip": "203.0.113.5" },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys).toEqual(["rl:ip:203.0.113.5"]);
  });

  it("trust=alb-xff-last は xff 末尾を信頼し、偽装 fly-client-ip を無視する (invariant 1)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: {
        "fly-client-ip": "6.6.6.6", // 攻撃者による偽装値
        "x-forwarded-for": "1.2.3.4, 5.6.7.8",
      },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys).toEqual(["rl:ip:5.6.7.8"]);
  });

  it("trust=alb-xff-last で xff 欠如なら fly-client-ip があっても fail-closed + missing_ip", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "fly-client-ip": "203.0.113.5" },
    });
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });

  it("CLIENT_IP_TRUST が未知値なら未設定と同じく fail-closed + missing_ip (invariant 2)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "bogus-mode");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "fly-client-ip": "203.0.113.5" },
    });
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });

  it("dev/test は CLIENT_IP_TRUST を無視し、xff 先頭値の現行 fallback を維持する (invariant 4)", async () => {
    // NODE_ENV=test (beforeEach 既定) のまま CLIENT_IP_TRUST=alb-xff-last を設定しても無視される。
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys).toEqual(["rl:ip:1.2.3.4"]); // 先頭値 (末尾ではない)
  });

  it("trust=alb-xff-last で xff 末尾が非IP (ip:port 形式) なら rl:ip キーを作らず missing_ip を出す (保証3: 解決値の IP 構文検証)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4, 203.0.113.7:8080" },
    });
    await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });
});

describe("proxy — x-vector-client-ip 内部ヘッダの上書き不変条件 (invariant 3)", () => {
  function forwardedHeader(res: Response, name: string): string | null {
    return res.headers.get(`x-middleware-request-${name}`);
  }

  it("解決できた IP を x-vector-client-ip として下流へ設定する", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "fly-client-ip");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: {
        cookie: "better-auth.session_token=AAAA", // auth-redirect を回避し forwarded headers を観測可能にする
        "fly-client-ip": "203.0.113.5",
      },
    });
    const res = await proxy(req);
    expect(forwardedHeader(res, "x-vector-client-ip")).toBe("203.0.113.5");
  });

  it("IPv6 は /64 正規化後の値を x-vector-client-ip に設定する (識別単位の正規化)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "fly-client-ip");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: {
        cookie: "better-auth.session_token=AAAA", // auth-redirect を回避し forwarded headers を観測可能にする
        "fly-client-ip": "2001:db8:aaaa:bbbb:1:2:3:4",
      },
    });
    const res = await proxy(req);
    expect(forwardedHeader(res, "x-vector-client-ip")).toBe(
      "2001:0db8:aaaa:bbbb:0000:0000:0000:0000",
    );
  });

  it("外来 x-vector-client-ip の偽装値を解決値で上書きする (attacker preset + trust=alb-xff-last)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: {
        cookie: "better-auth.session_token=AAAA", // auth-redirect を回避し forwarded headers を観測可能にする
        "x-vector-client-ip": "9.9.9.9", // 攻撃者による事前偽装
        "fly-client-ip": "6.6.6.6", // trust=alb-xff-last では無視される偽装値
        "x-forwarded-for": "1.2.3.4, 5.6.7.8",
      },
    });
    const res = await proxy(req);
    expect(forwardedHeader(res, "x-vector-client-ip")).toBe("5.6.7.8");
  });

  it("IP 未解決 (fail-closed) のときは外来 x-vector-client-ip を削除し、値を残さない", async () => {
    vi.stubEnv("NODE_ENV", "production");
    // CLIENT_IP_TRUST 未設定 → fail-closed
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: {
        cookie: "better-auth.session_token=AAAA", // auth-redirect を回避し forwarded headers を観測可能にする
        "x-vector-client-ip": "9.9.9.9",
      },
    });
    const res = await proxy(req);
    expect(forwardedHeader(res, "x-vector-client-ip")).toBeNull();
    const overrideHeaders =
      res.headers.get("x-middleware-override-headers") ?? "";
    expect(overrideHeaders.split(",")).not.toContain("x-vector-client-ip");
  });

  it("SSE route は rate limit を skip しても x-vector-client-ip の設定は行う", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "fly-client-ip");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest(
      "http://localhost:3000/api/research/runs/00000000-0000-4000-a000-000000000010/events",
      { headers: { "fly-client-ip": "203.0.113.5" } },
    );
    const res = await proxy(req);
    expect(mockEval).not.toHaveBeenCalled();
    expect(forwardedHeader(res, "x-vector-client-ip")).toBe("203.0.113.5");
  });
});

describe("proxy — health checker UA は missing_ip signal を抑制する (invariant 5)", () => {
  it("ELB-HealthChecker UA では IP 未解決でも missing_ip signal を出さない", async () => {
    vi.stubEnv("NODE_ENV", "production");
    // CLIENT_IP_TRUST 未設定 → fail-closed (通常なら missing_ip)
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "user-agent": "ELB-HealthChecker/2.0" },
    });
    const res = await proxy(req);
    expect(res.status).not.toBe(429); // rate limit 判定自体は従来どおり fail-open
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeUndefined();
  });

  it("同条件でも health checker UA でなければ missing_ip signal を出す (対照)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });

  it("health checker UA でも rate limit の判定自体はスキップしない (IP 解決時は通常どおり count する)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "fly-client-ip");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: {
        "user-agent": "ELB-HealthChecker/2.0",
        "fly-client-ip": "203.0.113.5",
      },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys).toEqual(["rl:ip:203.0.113.5"]);
  });

  it("health checker UA でも unknown_write signal は抑制されない (defect regression: missing_ip 以外まで抑制しない)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    // CLIENT_IP_TRUST 未設定 → fail-closed。session 無 & IP 未解決の anon mutation は unknown_write 終端。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/api/some-mutation", {
      method: "POST",
      headers: { "user-agent": "ELB-HealthChecker/2.0" },
    });
    await proxy(req);
    expect(findLoggedEvent("frontend_rate_limit_unknown_write")).toBeDefined();
  });
});

describe("proxy — CLIENT_IP_TRUST 未宣言時の専用 warn (設定漏れと経路異常の区別)", () => {
  it("production で CLIENT_IP_TRUST 未設定なら detail=unset の warn を1回出す", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    const event = findLoggedEvent("frontend_client_ip_trust_unconfigured");
    expect(event).toMatchObject({ detail: "unset" });
  });

  it("production で CLIENT_IP_TRUST が不正値なら detail=invalid の warn を出し、生の値は載せない", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "bogus-mode");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    const event = findLoggedEvent("frontend_client_ip_trust_unconfigured");
    expect(event).toMatchObject({ detail: "invalid" });
    expect(JSON.stringify(event)).not.toContain("bogus-mode");
  });

  it("同じ request でも missing_ip 信号は client_ip_trust_unconfigured warn と独立して出る", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    expect(
      findLoggedEvent("frontend_client_ip_trust_unconfigured"),
    ).toBeDefined();
    expect(findLoggedEvent("frontend_rate_limit_missing_ip")).toBeDefined();
  });

  it("2回目以降の request では出ない (プロセスごとに1回だけ)", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(mockNextRequest("http://localhost:3000/news"));
    await proxy(mockNextRequest("http://localhost:3000/watchlist"));

    expect(
      findLoggedEvents("frontend_client_ip_trust_unconfigured"),
    ).toHaveLength(1);
  });

  it("dev/test (非 production) では出ない", async () => {
    // NODE_ENV=test (beforeEach 既定)、CLIENT_IP_TRUST も未設定のまま。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news");
    await proxy(req);
    expect(
      findLoggedEvent("frontend_client_ip_trust_unconfigured"),
    ).toBeUndefined();
  });
});

describe("proxy — auth-redirect の挙動", () => {
  it("anon が protected page を叩くと /auth/login にリダイレクト", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/news", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    const res = await proxy(req);
    expect(res.status).toBe(307);
    const location = res.headers.get("location") ?? "";
    expect(location).toContain("/auth/login");
    expect(location).toContain("callbackUrl=%2Fnews");
  });

  it("anon が /auth/login を叩いても redirect しない (auth page は除外)", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/auth/login", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    const res = await proxy(req);
    expect(res.status).not.toBe(307);
  });

  it("anon が /api/auth/sign-in/email を叩いても redirect しない (API route は除外、Better Auth handler に任せる)", async () => {
    // /api/* には redirect を適用せず、anon の sign-in 経路を壊さない。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest(
      "http://localhost:3000/api/auth/sign-in/email",
      {
        method: "POST",
        headers: { "x-forwarded-for": "1.2.3.4" },
      },
    );
    const res = await proxy(req);
    expect(res.status).not.toBe(307);
  });
});

describe("proxy — XFF chain 観測のゲート (isProduction && trust=alb-xff-last && XFF あり)", () => {
  it("3条件をすべて満たす単一値 XFF は観測され、multiValueXffRequestCount は増えない", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4" },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4" },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toMatchObject({
      xffRequestCount: 2,
      multiValueXffRequestCount: 0,
    });
  });

  it("XFF が2値なら multiValueXffRequestCount も観測される", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toMatchObject({
      xffRequestCount: 2,
      multiValueXffRequestCount: 2,
    });
  });

  it("3値以上でも multiValue として数える (>=2 境界、===2 固定の回帰を防ぐ)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.9.9.9" },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.9.9.9" },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toMatchObject({
      multiValueXffRequestCount: 2,
    });
  });

  it("isProduction=false (dev/test) では trust と XFF が揃っても観測されない", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    // NODE_ENV=test (beforeEach 既定) のまま CLIENT_IP_TRUST だけ alb-xff-last にする。
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:05:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toBeUndefined();
  });

  it("trust=fly-client-ip では production かつ XFF ありでも観測されない (Fly の XFF は別構造なので混ぜない)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "fly-client-ip");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: {
          "fly-client-ip": "203.0.113.5",
          "x-forwarded-for": "1.2.3.4, 5.6.7.8",
        },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:05:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: {
          "fly-client-ip": "203.0.113.5",
          "x-forwarded-for": "1.2.3.4, 5.6.7.8",
        },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toBeUndefined();
  });

  it("CLIENT_IP_TRUST 未設定/不正値では production かつ XFF ありでも観測されない", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    // CLIENT_IP_TRUST を明示的に設定しない → 未宣言
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:05:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toBeUndefined();
  });
});

describe("proxy — XFF chain 観測: health check / 内部呼び出しは分母に入らない", () => {
  it("ELB-HealthChecker UA (XFF 無し) は分母に入らない", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "user-agent": "ELB-HealthChecker/2.0" },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:05:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: { "user-agent": "ELB-HealthChecker/2.0" },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toBeUndefined();
  });

  it("UA も XFF も無い内部呼び出し (service connect 相当) も分母に入らない", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(mockNextRequest("http://localhost:3000/news"));
    vi.setSystemTime(new Date("2026-01-01T00:05:00.000Z"));
    await proxy(mockNextRequest("http://localhost:3000/news"));

    expect(findLoggedEvent("frontend_xff_chain_observed")).toBeUndefined();
  });

  it("health checker UA でも XFF が付いていれば分母に入る (偽装可能な UA 判定はこの除外に使わない)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("CLIENT_IP_TRUST", "alb-xff-last");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);

    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: {
          "user-agent": "ELB-HealthChecker/2.0",
          "x-forwarded-for": "1.2.3.4",
        },
      }),
    );
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    await proxy(
      mockNextRequest("http://localhost:3000/news", {
        headers: {
          "user-agent": "ELB-HealthChecker/2.0",
          "x-forwarded-for": "1.2.3.4",
        },
      }),
    );

    expect(findLoggedEvent("frontend_xff_chain_observed")).toMatchObject({
      xffRequestCount: 2,
    });
  });
});
