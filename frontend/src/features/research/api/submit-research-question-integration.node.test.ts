import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  buildInternalAuthHeaders: vi.fn(),
  fetch: vi.fn(),
  getCurrentSession: vi.fn(),
  logServerEvent: vi.fn(),
  requireSessionForAction: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth/guards", () => ({
  getCurrentSession: mocks.getCurrentSession,
  requireSessionForAction: mocks.requireSessionForAction,
}));
vi.mock("@/lib/api/internal-config", () => ({
  INTERNAL_API_URL: "http://backend:8000/api/v1",
  buildBffRequestHeaders: vi.fn(),
  buildInternalAuthHeaders: mocks.buildInternalAuthHeaders,
}));
vi.mock("@/lib/observability/server-log", () => ({
  logServerEvent: mocks.logServerEvent,
}));

import { submitResearchQuestion } from "./submit-research-question";

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const RESET_AT = "2026-07-21T00:00:00+09:00";

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.requireSessionForAction.mockResolvedValue(undefined);
  mocks.getCurrentSession.mockResolvedValue({
    user: { id: "00000000-0000-4000-a000-000000000003", role: "user" },
  });
  mocks.buildInternalAuthHeaders.mockResolvedValue({
    Authorization: "Bearer test-token",
  });
});

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("submitResearchQuestion — BFF typed quota 429 integration", () => {
  it("backendの429本文とRetry-Afterを専用結果へ変換し、本文をログへ送らない", async () => {
    mocks.fetch.mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: "Daily research request limit exceeded",
          code: "research_daily_request_limit_exceeded",
          limit: 10,
          resetAt: RESET_AT,
        }),
        {
          status: 429,
          statusText: "Too Many Requests",
          headers: {
            "Content-Type": "application/json",
            "Retry-After": "37",
          },
        },
      ),
    );

    await expect(
      submitResearchQuestion("日次利用枠の確認", THREAD_ID),
    ).resolves.toEqual({
      kind: "daily-request-limit-exceeded",
      resetAt: RESET_AT,
      retryAfterSeconds: 37,
    });

    expect(mocks.fetch).toHaveBeenCalledOnce();
    expect(mocks.logServerEvent).toHaveBeenCalledWith(
      "warn",
      "frontend_internal_api_failure",
      {
        kind: "http_429",
        method: "POST",
        path: "/api/v1/research/responses",
        status: 429,
        detail: "Daily research request limit exceeded",
      },
    );
    const [, , loggedPayload] = mocks.logServerEvent.mock.calls[0] ?? [];
    expect(loggedPayload).toStrictEqual({
      kind: "http_429",
      method: "POST",
      path: "/api/v1/research/responses",
      status: 429,
      detail: "Daily research request limit exceeded",
    });
    expect(JSON.stringify(loggedPayload)).not.toContain(
      "research_daily_request_limit_exceeded",
    );
    expect(JSON.stringify(loggedPayload)).not.toContain("resetAt");
    expect(JSON.stringify(loggedPayload)).not.toContain('"limit":');
  });
});
