import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getSessionCookie: vi.fn(),
  getSession: vi.fn(),
  buildInternalAuthHeaders: vi.fn(),
  buildSseRateLimitPlan: vi.fn(),
  checkRateLimit: vi.fn(),
  recordRateLimitSignal: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("better-auth/cookies", () => ({
  getSessionCookie: mocks.getSessionCookie,
}));
vi.mock("@/lib/auth/auth", () => ({
  auth: { api: { getSession: mocks.getSession } },
}));
vi.mock("@/lib/api/internal-config", () => ({
  INTERNAL_API_URL: "http://backend:8000",
  buildInternalAuthHeaders: mocks.buildInternalAuthHeaders,
}));
vi.mock("@/lib/proxy/rate-limit-plan", () => ({
  buildSseRateLimitPlan: mocks.buildSseRateLimitPlan,
}));
vi.mock("@/lib/auth/rate-limit", () => ({
  checkRateLimit: mocks.checkRateLimit,
  recordRateLimitSignal: mocks.recordRateLimitSignal,
}));

import { GET } from "./route";

const RUN_ID = "00000000-0000-4000-a000-000000000010";
const URL = `http://test.local/api/research/runs/${RUN_ID}/events`;

function context(runId = RUN_ID) {
  return { params: Promise.resolve({ runId }) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.getSessionCookie.mockReturnValue("unverified-session-token");
  mocks.getSession.mockResolvedValue({
    user: { id: "user-1", role: "user" },
    session: { id: "session-1" },
  });
  mocks.buildInternalAuthHeaders.mockResolvedValue({
    Authorization: "Bearer internal-jwt",
  });
  mocks.buildSseRateLimitPlan.mockReturnValue({ tiers: [] });
  mocks.checkRateLimit.mockResolvedValue({ allowed: true });
});

describe("GET /api/research/runs/[runId]/events", () => {
  it.each([
    "bad-id",
    "../events",
    "00000000-0000-4000-a000-000000000010/extra",
  ])("rejects malformed run id %s before session and upstream", async (runId) => {
    const response = await GET(new Request(URL), context(runId));

    expect(response.status).toBe(400);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(mocks.getSession).not.toHaveBeenCalled();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it.each([
    "1",
    "1-0\r\nAuthorization: forged",
    "18446744073709551616-0",
    "0-18446744073709551616",
    `1-${"0".repeat(100)}`,
  ])("rejects malformed cursor %s before session and upstream", async (cursor) => {
    const request = cursor.includes("\r")
      ? ({
          headers: {
            get: (name: string) => (name === "Last-Event-ID" ? cursor : null),
          },
          signal: new AbortController().signal,
        } as unknown as Request)
      : new Request(URL, { headers: { "Last-Event-ID": cursor } });
    const response = await GET(request, context());

    expect(response.status).toBe(400);
    expect(mocks.getSession).not.toHaveBeenCalled();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("returns 401 without a Better Auth session", async () => {
    mocks.getSession.mockResolvedValue(null);

    const response = await GET(new Request(URL), context());

    expect(response.status).toBe(401);
    expect(mocks.checkRateLimit).toHaveBeenCalledBefore(mocks.getSession);
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("rejects the pre-auth SSE limit before reading the session store", async () => {
    mocks.buildSseRateLimitPlan.mockReturnValue({
      tiers: [{ key: "fixed", limit: 12 }],
      signal: "missing_ip",
    });
    mocks.checkRateLimit.mockResolvedValue({
      allowed: false,
      retryAfterSeconds: 37,
    });

    const response = await GET(
      new Request(URL, { headers: { "fly-client-ip": "203.0.113.5" } }),
      context(),
    );

    expect(mocks.buildSseRateLimitPlan).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionIdentity: "unverified-session-token",
        runId: RUN_ID,
      }),
    );
    expect(mocks.recordRateLimitSignal).toHaveBeenCalledWith("missing_ip");
    expect(mocks.checkRateLimit).toHaveBeenCalledWith(expect.anything(), {
      requestClass: "sse",
    });
    expect(response.status).toBe(429);
    expect(response.headers.get("Retry-After")).toBe("37");
    expect(mocks.getSession).not.toHaveBeenCalled();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("normalizes uppercase UUID before rate-limit key and upstream URL", async () => {
    const uppercaseRunId = RUN_ID.toUpperCase();
    mocks.fetch.mockResolvedValue(new Response(null, { status: 204 }));

    const response = await GET(new Request(URL), context(uppercaseRunId));

    expect(response.status).toBe(204);
    expect(mocks.buildSseRateLimitPlan).toHaveBeenCalledWith(
      expect.objectContaining({ runId: RUN_ID }),
    );
    expect(mocks.fetch).toHaveBeenCalledWith(
      `http://backend:8000/api/v1/research/runs/${RUN_ID}/events`,
      expect.anything(),
    );
  });

  it("forwards JWT, Accept, cursor, and the unbuffered response body", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("retry: 1000\n\n"));
        controller.close();
      },
    });
    mocks.fetch.mockResolvedValue(
      new Response(stream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream; charset=utf-8" },
      }),
    );

    const response = await GET(
      new Request(URL, { headers: { "Last-Event-ID": "9-0" } }),
      context(),
    );

    const [upstreamUrl, init] = mocks.fetch.mock.calls[0] as [
      string,
      RequestInit,
    ];
    const headers = new Headers(init.headers);
    expect(upstreamUrl).toBe(
      `http://backend:8000/api/v1/research/runs/${RUN_ID}/events`,
    );
    expect(headers.get("Authorization")).toBe("Bearer internal-jwt");
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(headers.get("Last-Event-ID")).toBe("9-0");
    expect(init.cache).toBe("no-store");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(response.body).toBe(stream);
    expect(response.headers.get("Cache-Control")).toBe(
      "no-store, no-transform",
    );
    expect(response.headers.get("Content-Encoding")).toBeNull();
    expect(response.headers.get("X-Accel-Buffering")).toBe("no");
  });

  it.each([
    204, 409, 429, 503,
  ])("passes through backend status %s and Retry-After without body conversion", async (status) => {
    const body = status === 204 ? null : new ReadableStream<Uint8Array>();
    mocks.fetch.mockResolvedValue(
      new Response(body, {
        status,
        headers: status === 204 ? {} : { "Retry-After": "5" },
      }),
    );

    const response = await GET(new Request(URL), context());

    expect(response.status).toBe(status);
    expect(response.headers.get("Cache-Control")).toBe(
      "no-store, no-transform",
    );
    expect(response.headers.get("Retry-After")).toBe(
      status === 204 ? null : "5",
    );
    if (status === 204) expect(response.body).toBeNull();
  });

  it("silently handles browser abort without an unhandled fetch rejection", async () => {
    const controller = new AbortController();
    mocks.fetch.mockImplementation(async (_url, init: RequestInit) => {
      const signal = init.signal as AbortSignal;
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener("abort", () =>
          reject(new DOMException("", "AbortError")),
        );
      });
      return new Response(null, { status: 204 });
    });
    const pending = GET(
      new Request(URL, { signal: controller.signal }),
      context(),
    );
    await vi.waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());

    controller.abort();

    await expect(pending).resolves.toMatchObject({ status: 204 });
  });

  it("uses a 50 second hard timeout that aborts the upstream signal", async () => {
    const timeoutController = new AbortController();
    const timeoutSpy = vi
      .spyOn(AbortSignal, "timeout")
      .mockReturnValue(timeoutController.signal);
    mocks.fetch.mockImplementation(async (_url, init: RequestInit) => {
      const signal = init.signal as AbortSignal;
      await new Promise<void>((_resolve, reject) => {
        signal.addEventListener("abort", () =>
          reject(new DOMException("", "AbortError")),
        );
      });
      return new Response(null, { status: 204 });
    });
    try {
      const pending = GET(new Request(URL), context());
      await vi.waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
      const signal = mocks.fetch.mock.calls[0]?.[1]?.signal as AbortSignal;

      expect(timeoutSpy).toHaveBeenCalledWith(50_000);
      expect(signal.aborted).toBe(false);
      timeoutController.abort();
      expect(signal.aborted).toBe(true);

      const response = await pending;
      expect(response.status).toBe(503);
      expect(response.headers.get("Retry-After")).toBe("5");
    } finally {
      timeoutSpy.mockRestore();
    }
  });

  it("maps upstream network failure to retryable 503", async () => {
    mocks.fetch.mockRejectedValue(new TypeError("SECRET upstream failed"));

    const response = await GET(new Request(URL), context());

    expect(response.status).toBe(503);
    expect(response.headers.get("Retry-After")).toBe("5");
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });
});
