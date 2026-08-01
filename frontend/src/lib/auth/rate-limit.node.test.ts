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
import type { RateLimitTier } from "@/lib/proxy/rate-limit-plan";
import {
  calculateLimits,
  checkRateLimit,
  parseLimit,
  recordRateLimitSignal,
  recordXffChainObservation,
} from "./rate-limit";

const g = globalThis as unknown as {
  __vectorRateLimitRedis?: unknown;
  __vectorRateLimitErrorLastMs?: number;
  __vectorRateLimitSignalLastMs?: Record<string, number>;
  __vectorRateLimitFailOpenLastMs?: Record<string, number>;
  __vectorRateLimitMisconfigLogged?: boolean;
  __vectorXffChainWindow?: unknown;
};

let warnSpy: MockInstance<typeof console.warn>;

function plan(...tiers: RateLimitTier[]) {
  return { tiers };
}

type WarnPayload = Record<string, unknown>;

function warnPayloads(event: string): WarnPayload[] {
  return warnSpy.mock.calls
    .map((call) => JSON.parse(call[0] as string) as WarnPayload)
    .filter((payload) => payload.event === event);
}

function warnPayload(event: string): WarnPayload {
  const [payload] = warnPayloads(event);
  if (!payload) {
    throw new Error(`no warn payload recorded for event "${event}"`);
  }
  return payload;
}

beforeEach(() => {
  // singleton state を test 間で reset (HMR/test isolation 用に globalThis を採用しているため)
  delete g.__vectorRateLimitRedis;
  delete g.__vectorRateLimitErrorLastMs;
  delete g.__vectorRateLimitSignalLastMs;
  delete g.__vectorRateLimitFailOpenLastMs;
  delete g.__vectorRateLimitMisconfigLogged;
  delete g.__vectorXffChainWindow;
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

describe("calculateLimits", () => {
  it("returns the new defaults (rsc 600 / session 60 / ip 300 / unknownWrite 30)", () => {
    expect(calculateLimits({})).toEqual({
      rsc: 600,
      session: 60,
      ip: 300,
      unknownWrite: 30,
    });
  });

  it("respects each env override independently", () => {
    expect(
      calculateLimits({
        RATE_LIMIT_RSC_PER_MIN: "900",
        RATE_LIMIT_SESSION_PER_MIN: "80",
        RATE_LIMIT_IP_PER_MIN: "400",
        RATE_LIMIT_UNKNOWN_WRITE_PER_MIN: "15",
      }),
    ).toEqual({ rsc: 900, session: 80, ip: 400, unknownWrite: 15 });
  });

  it("falls back to default per-field for invalid override", () => {
    expect(
      calculateLimits({
        RATE_LIMIT_RSC_PER_MIN: "not-a-number",
        RATE_LIMIT_SESSION_PER_MIN: "0",
        RATE_LIMIT_IP_PER_MIN: "-5",
      }),
    ).toEqual({ rsc: 600, session: 60, ip: 300, unknownWrite: 30 });
  });

  it("ignores the deprecated RATE_LIMIT_PER_MIN env (regression guard)", () => {
    // 旧単一 env を渡しても新 4 field は default のまま (読まないことを担保)。
    expect(calculateLimits({ RATE_LIMIT_PER_MIN: "999" })).toEqual({
      rsc: 600,
      session: 60,
      ip: 300,
      unknownWrite: 30,
    });
  });
});

describe("checkRateLimit", () => {
  it("returns allowed without calling eval when tiers is empty (structural fail-open)", async () => {
    mockIsOpenValue = true;
    const decision = await checkRateLimit(plan());
    expect(decision).toEqual({ allowed: true });
    expect(mockEval).not.toHaveBeenCalled();
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it("returns allowed when both REDIS_URL_RL and REDIS_URL are unset (fail open)", async () => {
    vi.stubEnv("REDIS_URL_RL", "");
    vi.stubEnv("REDIS_URL", "");
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: true });
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it("calls connect on first invocation when client is not open", async () => {
    mockIsOpenValue = false;
    mockEval.mockResolvedValue(1);
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    expect(mockConnect).toHaveBeenCalledOnce();
  });

  it("returns allowed when Lua script returns 1", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: true });
  });

  it("returns denied with retryAfterSeconds=60 when Lua script returns 0", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(0);
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: false, retryAfterSeconds: 60 });
  });

  it("single tier: keys と arguments の並び (windowMs / ttlSec / limit / uniqueId 末尾)", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    const call = mockEval.mock.calls[0]?.[1] as {
      keys: string[];
      arguments: string[];
    };
    expect(call.keys).toEqual(["rl:ip:1.2.3.4"]);
    expect(call.arguments[1]).toBe("60000"); // windowMs
    expect(call.arguments[2]).toBe("60"); // ttlSec
    expect(call.arguments[3]).toBe("300"); // tier limit
    expect(call.arguments).toHaveLength(5); // now, window, ttl, limit, uniqueId
  });

  it("multiple tiers: keys 順序保持・limit が末尾 2 個・uniqueId が最後", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit(
      plan(
        { key: "rl:sess:abcd1234abcd1234", limit: 60 },
        { key: "rl:ip:1.2.3.4", limit: 300 },
      ),
    );
    const call = mockEval.mock.calls[0]?.[1] as {
      keys: string[];
      arguments: string[];
    };
    expect(call.keys).toEqual(["rl:sess:abcd1234abcd1234", "rl:ip:1.2.3.4"]);
    expect(call.arguments[3]).toBe("60"); // 第1 tier limit
    expect(call.arguments[4]).toBe("300"); // 第2 tier limit
    expect(call.arguments).toHaveLength(6); // now, window, ttl, limit1, limit2, uniqueId
  });

  it("fails open and warns when eval throws", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: true });
    expect(warnSpy).toHaveBeenCalledOnce();
    const payload = JSON.parse(warnSpy.mock.calls[0]?.[0] as string);
    expect(payload).toMatchObject({
      event: "frontend_rate_limit_redis_fail_open",
      requestClass: "read",
      errorType: "eval",
    });
    expect(JSON.stringify(payload)).not.toContain("redis down");
  });

  it("records SSE fail-open with only fixed low-cardinality fields", async () => {
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("SECRET redis://private"));

    await checkRateLimit(plan({ key: "rl:sse:session:hash", limit: 30 }), {
      requestClass: "sse",
    });

    const payload = JSON.parse(warnSpy.mock.calls[0]?.[0] as string);
    expect(payload).toMatchObject({
      event: "frontend_rate_limit_redis_fail_open",
      requestClass: "sse",
      errorType: "eval",
    });
    expect(JSON.stringify(payload)).not.toContain("SECRET");
    expect(JSON.stringify(payload)).not.toContain("private");
    expect(JSON.stringify(payload)).not.toContain("rl:sse");
  });

  it("suppresses repeat warnings within 60 seconds (window-bounded log)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    expect(warnSpy).toHaveBeenCalledOnce();
    // 30 秒後の再失敗は抑制
    vi.setSystemTime(new Date("2026-01-01T00:00:30Z"));
    await checkRateLimit(plan({ key: "rl:ip:5.6.7.8", limit: 300 }));
    expect(warnSpy).toHaveBeenCalledOnce();
  });

  it("re-warns after 60 second interval (Redis 永続障害の見える化)", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    mockIsOpenValue = true;
    mockEval.mockRejectedValue(new Error("redis down"));
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    expect(warnSpy).toHaveBeenCalledTimes(1);
    // 60 秒経過後は再ログを許可
    vi.setSystemTime(new Date("2026-01-01T00:01:00Z"));
    await checkRateLimit(plan({ key: "rl:ip:5.6.7.8", limit: 300 }));
    expect(warnSpy).toHaveBeenCalledTimes(2);
  });

  it("registers an error handler on the redis client that warns once", async () => {
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
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
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    await checkRateLimit(plan({ key: "rl:ip:5.6.7.8", limit: 300 }));
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
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    expect(createClient).toHaveBeenCalledWith({ url: "redis://legacy:6379/1" });
  });

  it("prefers REDIS_URL_RL over REDIS_URL when both are set", async () => {
    vi.stubEnv("REDIS_URL_RL", "redis://rl:6379/0");
    vi.stubEnv("REDIS_URL", "redis://legacy:6379/1");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    expect(createClient).toHaveBeenCalledWith({ url: "redis://rl:6379/0" });
  });
});

describe("checkRateLimit — REDIS_IAM_AUTH 配線 (redis-iam-auth.ts)", () => {
  it("正しい設定なら createClient に userinfo 無し URL + credentialsProvider を渡す", async () => {
    vi.stubEnv("REDIS_IAM_AUTH", "true");
    vi.stubEnv(
      "REDIS_URL_RL",
      "rediss://vector_rl@vector-rate-limit.example.apne1.cache.amazonaws.com:6379/0",
    );
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "vector-rate-limit");
    vi.stubEnv("AWS_REGION", "ap-northeast-1");
    mockIsOpenValue = true;
    mockEval.mockResolvedValue(1);
    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    expect(createClient).toHaveBeenCalledWith({
      url: "rediss://vector-rate-limit.example.apne1.cache.amazonaws.com:6379/0",
      credentialsProvider: {
        type: "async-credentials-provider",
        credentials: expect.any(Function),
      },
    });
  });

  it("URL に username が無い誤設定は null client 扱いになり allow + unconfigured warn (throw を漏らさない)", async () => {
    vi.stubEnv("REDIS_IAM_AUTH", "true");
    vi.stubEnv(
      "REDIS_URL_RL",
      "rediss://vector-rate-limit.example.apne1.cache.amazonaws.com:6379/0",
    );
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "vector-rate-limit");
    vi.stubEnv("AWS_REGION", "ap-northeast-1");
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: true });
    expect(createClient).not.toHaveBeenCalled();
    const payload = warnPayload("frontend_rate_limit_redis_fail_open");
    expect(payload).toMatchObject({
      requestClass: "read",
      errorType: "unconfigured",
    });
  });

  it("REDIS_IAM_CACHE_NAME_RL 欠落の誤設定も同様に allow + unconfigured warn に倒す", async () => {
    vi.stubEnv("REDIS_IAM_AUTH", "true");
    vi.stubEnv(
      "REDIS_URL_RL",
      "rediss://vector_rl@vector-rate-limit.example.apne1.cache.amazonaws.com:6379/0",
    );
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "");
    vi.stubEnv("AWS_REGION", "ap-northeast-1");
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: true });
    expect(createClient).not.toHaveBeenCalled();
    const payload = warnPayload("frontend_rate_limit_redis_fail_open");
    expect(payload).toMatchObject({ errorType: "unconfigured" });
  });

  it("AWS_REGION 欠落の誤設定も同様に allow + unconfigured warn に倒す", async () => {
    vi.stubEnv("REDIS_IAM_AUTH", "true");
    vi.stubEnv(
      "REDIS_URL_RL",
      "rediss://vector_rl@vector-rate-limit.example.apne1.cache.amazonaws.com:6379/0",
    );
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "vector-rate-limit");
    vi.stubEnv("AWS_REGION", "");
    const decision = await checkRateLimit(
      plan({ key: "rl:ip:1.2.3.4", limit: 300 }),
    );
    expect(decision).toEqual({ allowed: true });
    expect(createClient).not.toHaveBeenCalled();
    const payload = warnPayload("frontend_rate_limit_redis_fail_open");
    expect(payload).toMatchObject({ errorType: "unconfigured" });
  });

  it("誤設定を2回起こしても misconfigured warn はプロセスごとに1回だけ、detail に診断が入る (unconfigured fail-open warn は従来どおり出続ける)", async () => {
    vi.stubEnv("REDIS_IAM_AUTH", "true");
    vi.stubEnv(
      "REDIS_URL_RL",
      "rediss://vector_rl@vector-rate-limit.example.apne1.cache.amazonaws.com:6379/0",
    );
    vi.stubEnv("REDIS_IAM_CACHE_NAME_RL", "");
    vi.stubEnv("AWS_REGION", "ap-northeast-1");

    await checkRateLimit(plan({ key: "rl:ip:1.2.3.4", limit: 300 }));
    await checkRateLimit(plan({ key: "rl:ip:5.6.7.8", limit: 300 }));

    const misconfigured = warnPayloads(
      "frontend_rate_limit_redis_misconfigured",
    );
    expect(misconfigured).toHaveLength(1);
    expect(String(misconfigured[0]?.detail)).toContain(
      "REDIS_IAM_CACHE_NAME_RL",
    );

    const failOpen = warnPayloads("frontend_rate_limit_redis_fail_open");
    expect(failOpen.length).toBeGreaterThanOrEqual(1);
    expect(failOpen[0]).toMatchObject({ errorType: "unconfigured" });
  });
});

describe("recordRateLimitSignal — per-signal throttle", () => {
  it("emits a warn-level log event for missing_ip", () => {
    recordRateLimitSignal("missing_ip");
    expect(warnSpy).toHaveBeenCalledOnce();
    const payload = JSON.parse(warnSpy.mock.calls[0]?.[0] as string);
    expect(payload.event).toBe("frontend_rate_limit_missing_ip");
    expect(payload.level).toBe("warn");
  });

  it("emits a distinct event for unknown_write", () => {
    recordRateLimitSignal("unknown_write");
    const payload = JSON.parse(warnSpy.mock.calls[0]?.[0] as string);
    expect(payload.event).toBe("frontend_rate_limit_unknown_write");
  });

  it("suppresses the same signal within 60s and re-emits after 60s", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    recordRateLimitSignal("missing_ip");
    expect(warnSpy).toHaveBeenCalledTimes(1);
    // 30 秒後の同 signal は抑制
    vi.setSystemTime(new Date("2026-01-01T00:00:30Z"));
    recordRateLimitSignal("missing_ip");
    expect(warnSpy).toHaveBeenCalledTimes(1);
    // 60 秒経過後は再 emit
    vi.setSystemTime(new Date("2026-01-01T00:01:00Z"));
    recordRateLimitSignal("missing_ip");
    expect(warnSpy).toHaveBeenCalledTimes(2);
  });

  it("throttles each signal kind independently", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    recordRateLimitSignal("missing_ip");
    recordRateLimitSignal("unknown_write");
    // 種別が独立なので両方 emit される
    expect(warnSpy).toHaveBeenCalledTimes(2);
  });
});

describe("recordXffChainObservation — ウィンドウ集計と throttle", () => {
  it("interval (60000ms) 未満の経過ではまだ出さない (蓄積のみ)", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    recordXffChainObservation(false);
    vi.setSystemTime(new Date("2026-01-01T00:00:59.999Z"));
    recordXffChainObservation(true);
    expect(warnPayloads("frontend_xff_chain_observed")).toHaveLength(0);
  });

  it("interval 経過後の呼び出しで、蓄積した2数を1行にまとめて出す", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    recordXffChainObservation(false); // single
    recordXffChainObservation(true); // multi
    recordXffChainObservation(true); // multi
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z")); // ちょうど60000ms経過
    recordXffChainObservation(false); // このウィンドウの4件目、かつ flush の契機

    expect(warnPayload("frontend_xff_chain_observed")).toStrictEqual({
      event: "frontend_xff_chain_observed",
      level: "warn",
      xffRequestCount: 4,
      multiValueXffRequestCount: 2,
    });
  });

  it("multiValueXffRequestCount が0でも省略せず出す (0件とデータ無しを区別する、recordRateLimitSignal との意図的な差)", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    recordXffChainObservation(false);
    recordXffChainObservation(false);
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    recordXffChainObservation(false);

    const payload = warnPayload("frontend_xff_chain_observed");
    expect(payload.xffRequestCount).toBe(3);
    expect(payload.multiValueXffRequestCount).toBe(0);
  });

  it("emit 後はカウンタが0にリセットされ、直後の呼び出しでは再度出さない", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    recordXffChainObservation(true);
    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    recordXffChainObservation(true); // 1回目の flush
    expect(warnPayloads("frontend_xff_chain_observed")).toHaveLength(1);

    recordXffChainObservation(true); // reset 直後、同時刻の呼び出し
    expect(warnPayloads("frontend_xff_chain_observed")).toHaveLength(1);
  });

  it("複数回のウィンドウ境界をまたいでも、毎回リセットされた値だけを集計する", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00.000Z"));
    recordXffChainObservation(true);

    vi.setSystemTime(new Date("2026-01-01T00:01:00.000Z"));
    recordXffChainObservation(false); // 1回目の flush (window1: true+false)

    vi.setSystemTime(new Date("2026-01-01T00:02:00.000Z"));
    recordXffChainObservation(true); // 2回目の flush (window2: true のみ)

    const payloads = warnPayloads("frontend_xff_chain_observed");
    expect(payloads).toHaveLength(2);
    expect(payloads[0]).toMatchObject({
      xffRequestCount: 2,
      multiValueXffRequestCount: 1,
    });
    expect(payloads[1]).toMatchObject({
      xffRequestCount: 1,
      multiValueXffRequestCount: 1,
    });
  });
});
