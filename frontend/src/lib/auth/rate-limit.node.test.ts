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

// redis mock — module load 前に hoist させるため top-level に置く
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

import { createClient } from "redis";
import { calculateLimit, checkRateLimit, parseLimit } from "./rate-limit";

const g = globalThis as unknown as {
  __vectorRateLimitRedis?: unknown;
  __vectorRateLimitErrorLogged?: boolean;
};

let warnSpy: MockInstance<typeof console.warn>;

beforeEach(() => {
  // singleton state を test 間で reset (HMR/test isolation 用に globalThis を採用しているため)
  delete g.__vectorRateLimitRedis;
  delete g.__vectorRateLimitErrorLogged;
  mockEval.mockReset();
  mockConnect.mockReset();
  mockOn.mockReset();
  vi.mocked(createClient).mockClear();
  mockIsOpenValue = false;
  vi.stubEnv("REDIS_URL", "redis://test:6379/1");
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  warnSpy.mockRestore();
});

describe("parseLimit", () => {
  it("returns fallback when raw is undefined", () => {
    expect(parseLimit(undefined, 120)).toBe(120);
  });

  it("returns fallback when raw is empty string", () => {
    expect(parseLimit("", 120)).toBe(120);
  });

  it("returns fallback for non-numeric", () => {
    expect(parseLimit("abc", 60)).toBe(60);
  });

  it("returns fallback for zero", () => {
    expect(parseLimit("0", 60)).toBe(60);
  });

  it("returns fallback for negative", () => {
    expect(parseLimit("-5", 60)).toBe(60);
  });

  it("parses positive integer", () => {
    expect(parseLimit("250", 60)).toBe(250);
  });

  it("parses leading-integer string", () => {
    // Number.parseInt が前方一致で読むため、これは意図的な許容
    expect(parseLimit("100abc", 60)).toBe(100);
  });
});

describe("calculateLimit", () => {
  it("returns 120 for auth by default", () => {
    expect(calculateLimit("auth", {})).toBe(120);
  });

  it("returns 60 for anon by default", () => {
    expect(calculateLimit("anon", {})).toBe(60);
  });

  it("respects RATE_LIMIT_AUTHED_PER_MIN override", () => {
    expect(calculateLimit("auth", { RATE_LIMIT_AUTHED_PER_MIN: "300" })).toBe(
      300,
    );
  });

  it("respects RATE_LIMIT_ANON_PER_MIN override", () => {
    expect(calculateLimit("anon", { RATE_LIMIT_ANON_PER_MIN: "30" })).toBe(30);
  });

  it("falls back to default for invalid auth override", () => {
    expect(
      calculateLimit("auth", { RATE_LIMIT_AUTHED_PER_MIN: "not-a-number" }),
    ).toBe(120);
  });

  it("falls back to default for invalid anon override", () => {
    expect(calculateLimit("anon", { RATE_LIMIT_ANON_PER_MIN: "0" })).toBe(60);
  });
});

describe("checkRateLimit", () => {
  it("returns allowed when REDIS_URL is unset (fail open)", async () => {
    vi.stubEnv("REDIS_URL", "");
    const decision = await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    expect(decision).toEqual({ allowed: true });
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it("calls connect on first invocation when client is not open", async () => {
    mockIsOpenValue = false;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    expect(mockConnect).toHaveBeenCalledOnce();
  });

  it("returns allowed when Lua script returns 1", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const decision = await checkRateLimit({ kind: "auth", key: "deadbeef" });
    expect(decision).toEqual({ allowed: true });
  });

  it("returns denied with retryAfterSeconds=60 when Lua script returns 0", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(0);
    const decision = await checkRateLimit({ kind: "auth", key: "deadbeef" });
    expect(decision).toEqual({ allowed: false, retryAfterSeconds: 60 });
  });

  it("fails open and warns when eval throws", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    const decision = await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    expect(decision).toEqual({ allowed: true });
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("logs warning only once across multiple failures (deduplicates)", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    await checkRateLimit({ kind: "anon", key: "5.6.7.8" });
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("uses rl:auth:<key> namespace for auth identifier", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "auth", key: "deadbeef1234" });
    const call = mockEval.mock.calls[0];
    expect(call?.[1]).toMatchObject({ keys: ["rl:auth:deadbeef1234"] });
  });

  it("uses rl:anon:<key> namespace for anon identifier", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    const call = mockEval.mock.calls[0];
    expect(call?.[1]).toMatchObject({ keys: ["rl:anon:1.2.3.4"] });
  });

  it("passes default auth limit (120) as the third argument", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "auth", key: "deadbeef" });
    const args = mockEval.mock.calls[0]?.[1].arguments as string[];
    expect(args[2]).toBe("120");
  });

  it("passes default anon limit (60) as the third argument", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    const args = mockEval.mock.calls[0]?.[1].arguments as string[];
    expect(args[2]).toBe("60");
  });

  it("registers an error handler on the redis client that warns once", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "auth", key: "abc" });
    const errorRegistration = mockOn.mock.calls.find(
      (call) => call[0] === "error",
    );
    expect(errorRegistration).toBeDefined();
    const handler = errorRegistration?.[1] as (err: unknown) => void;
    handler(new Error("connection lost"));
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("reuses the cached client across calls (singleton)", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "anon", key: "1.2.3.4" });
    await checkRateLimit({ kind: "anon", key: "5.6.7.8" });
    // createClient は最初の 1 回だけ呼ばれる (singleton)
    expect(createClient).toHaveBeenCalledOnce();
  });
});
