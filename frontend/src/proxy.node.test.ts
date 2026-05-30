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
};

let warnSpy: MockInstance<typeof console.warn>;

beforeEach(() => {
  delete g.__vectorRateLimitRedis;
  delete g.__vectorRateLimitErrorLastMs;
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

describe("proxy — red-team C1 5 経路 bypass 防止 (構造的 regression)", () => {
  it("(1) /api/auth/sign-in/email POST anon は matcher 対象内で rate-limit が走る", async () => {
    // /api/auth/* も rate-limit を経由するが、
    // handler には NextResponse.next() で透過する。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest(
      "http://localhost:3000/api/auth/sign-in/email",
      {
        method: "POST",
        headers: { "x-forwarded-for": "1.2.3.4" },
      },
    );
    await proxy(req);
    expect(mockEval).toHaveBeenCalledTimes(1);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys[0]).toBe("rl:ip:1.2.3.4");
  });

  it("(2) cookie/XFF/X-Real-IP すべて欠如の anon GET は 'unknown' bucket で rate-limit が走る", async () => {
    // IP source 欠如時も "unknown" bucket で rate-limit を走らせる。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/");
    await proxy(req);
    expect(mockEval).toHaveBeenCalledTimes(1);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys[0]).toBe("rl:ip:unknown");
  });

  it("(3) 上限超過時は 429 with Retry-After を返す (bucket 飽和)", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(0); // Lua script が denied を返す
    const req = mockNextRequest("http://localhost:3000/", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    const res = await proxy(req);
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("60");
  });

  it("(4) cookie present + XFF なしでも identifier は IP-based ('unknown' bucket、cookie 値で別 bucket にしない / F4 対策)", async () => {
    // cookie 値は identifier に使わず、IP source 欠如時は "unknown" bucket になる。
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const req = mockNextRequest("http://localhost:3000/", {
      headers: { cookie: "better-auth.session_token=AAAA" },
    });
    await proxy(req);
    const args = mockEval.mock.calls[0]?.[1] as { keys: string[] };
    expect(args.keys[0]).toBe("rl:ip:unknown");
  });

  it("(5) Redis 障害時は fail-open で透過し warn を 1 度出す", async () => {
    // Redis 障害は warn を出しつつ fail-open する。
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    const req = mockNextRequest("http://localhost:3000/", {
      headers: { "x-forwarded-for": "1.2.3.4" },
    });
    const res = await proxy(req);
    expect(res.status).not.toBe(429);
    expect(warnSpy).toHaveBeenCalledOnce();
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
