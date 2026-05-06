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
  __vectorRateLimitErrorLastMs?: number;
};

let warnSpy: MockInstance<typeof console.warn>;

beforeEach(() => {
  // singleton state を test 間で reset (HMR/test isolation 用に globalThis を採用しているため)
  delete g.__vectorRateLimitRedis;
  delete g.__vectorRateLimitErrorLastMs;
  mockEval.mockReset();
  mockConnect.mockReset();
  mockOn.mockReset();
  vi.mocked(createClient).mockClear();
  mockIsOpenValue = false;
  vi.stubEnv("REDIS_URL_RL", "redis://test-rl:6379/0");
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  warnSpy.mockRestore();
  vi.useRealTimers();
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
  it("returns the default limit (60) when no override is set", () => {
    expect(calculateLimit({})).toBe(60);
  });

  it("respects RATE_LIMIT_PER_MIN override", () => {
    expect(calculateLimit({ RATE_LIMIT_PER_MIN: "300" })).toBe(300);
  });

  it("falls back to default for invalid override", () => {
    expect(calculateLimit({ RATE_LIMIT_PER_MIN: "not-a-number" })).toBe(60);
  });

  it("falls back to default for zero override", () => {
    expect(calculateLimit({ RATE_LIMIT_PER_MIN: "0" })).toBe(60);
  });
});

describe("checkRateLimit", () => {
  it("returns allowed when both REDIS_URL_RL and REDIS_URL are unset (fail open)", async () => {
    vi.stubEnv("REDIS_URL_RL", "");
    vi.stubEnv("REDIS_URL", "");
    const decision = await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(decision).toEqual({ allowed: true });
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it("calls connect on first invocation when client is not open", async () => {
    mockIsOpenValue = false;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(mockConnect).toHaveBeenCalledOnce();
  });

  it("returns allowed when Lua script returns 1", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const decision = await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(decision).toEqual({ allowed: true });
  });

  it("returns denied with retryAfterSeconds=60 when Lua script returns 0", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(0);
    const decision = await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(decision).toEqual({ allowed: false, retryAfterSeconds: 60 });
  });

  it("fails open and warns when eval throws", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    const decision = await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(decision).toEqual({ allowed: true });
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("suppresses repeat warnings within 60 seconds (window-bounded log)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(warnSpy).toHaveBeenCalledOnce();
    // 30 秒後の再失敗は抑制
    vi.setSystemTime(new Date("2026-01-01T00:00:30Z"));
    await checkRateLimit({ kind: "ip", key: "5.6.7.8" });
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("re-warns after 60 second interval (Redis 永続障害の見える化)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    // 60 秒経過後は再ログを許可
    vi.setSystemTime(new Date("2026-01-01T00:01:00Z"));
    await checkRateLimit({ kind: "ip", key: "5.6.7.8" });
    expect(warnSpy).toHaveBeenCalledTimes(2);
  });

  it("uses rl:ip:<key> namespace for ip identifier", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    const call = mockEval.mock.calls[0];
    expect(call?.[1]).toMatchObject({ keys: ["rl:ip:1.2.3.4"] });
  });

  it("passes default limit (60) as the third argument", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    const args = mockEval.mock.calls[0]?.[1].arguments as string[];
    expect(args[2]).toBe("60");
  });

  it("respects RATE_LIMIT_PER_MIN env override at call time", async () => {
    vi.stubEnv("RATE_LIMIT_PER_MIN", "200");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    const args = mockEval.mock.calls[0]?.[1].arguments as string[];
    expect(args[2]).toBe("200");
  });

  it("registers an error handler on the redis client that warns once", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
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
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    await checkRateLimit({ kind: "ip", key: "5.6.7.8" });
    // createClient は最初の 1 回だけ呼ばれる (singleton)
    expect(createClient).toHaveBeenCalledOnce();
  });
});

// red-team C9 対策: REDIS_URL_RL を優先採用し、未設定時のみ REDIS_URL に
// フォールバックする構造的不変条件を test で担保。?? を || に書き換える等の
// regression を防止する目的。
describe("checkRateLimit — REDIS_URL_RL fallback (PR1 C9 対策)", () => {
  it("falls back to REDIS_URL when REDIS_URL_RL is unset", async () => {
    vi.stubEnv("REDIS_URL_RL", "");
    vi.stubEnv("REDIS_URL", "redis://legacy:6379/1");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(createClient).toHaveBeenCalledWith({ url: "redis://legacy:6379/1" });
  });

  it("prefers REDIS_URL_RL over REDIS_URL when both are set", async () => {
    vi.stubEnv("REDIS_URL_RL", "redis://rl:6379/0");
    vi.stubEnv("REDIS_URL", "redis://legacy:6379/1");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit({ kind: "ip", key: "1.2.3.4" });
    expect(createClient).toHaveBeenCalledWith({ url: "redis://rl:6379/0" });
  });
});
