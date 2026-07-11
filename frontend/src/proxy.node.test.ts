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
};

let warnSpy: MockInstance<typeof console.warn>;

beforeEach(() => {
  delete g.__vectorRateLimitRedis;
  delete g.__vectorRateLimitErrorLastMs;
  delete g.__vectorRateLimitSignalLastMs;
  delete g.__vectorRateLimitFailOpenLastMs;
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

  it("production は fly 欠如時に xff を信頼せず、anon read は fail-open する", async () => {
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
