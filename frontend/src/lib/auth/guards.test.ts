import { beforeEach, describe, expect, it, vi } from "vitest";

// Next.js framework + Better Auth は実体に触れず、すべて vi.mock で置換する。
// 真の検証対象は guards.ts の分岐ロジック (session の有無 / role の正規化)。
//
// `vi.mock` は file top に hoist されるため、参照する mock 関数は `vi.hoisted`
// で先に巻き上げる必要がある (top-level const では「初期化前にアクセス」エラー)。

vi.mock("server-only", () => ({}));

const mocks = vi.hoisted(() => ({
  headersGet: vi.fn<(name: string) => string | null>(),
  redirect: vi.fn((url: string) => {
    // Next.js の redirect は NEXT_REDIRECT 特殊 throw で制御を返さない。
    // テストでは「呼ばれた + 引数」を保証する代わりに throw で制御を切る。
    throw new Error(`REDIRECT:${url}`);
  }),
  getSession: vi.fn(),
  logServerEvent: vi.fn(),
}));

vi.mock("next/headers", () => ({
  headers: vi.fn(async () => ({ get: mocks.headersGet })),
}));

vi.mock("next/navigation", () => ({
  redirect: mocks.redirect,
}));

vi.mock("@/lib/auth/auth", () => ({
  auth: { api: { getSession: mocks.getSession } },
}));

vi.mock("@/lib/observability/server-log", () => ({
  logServerEvent: mocks.logServerEvent,
}));

const {
  headersGet: headersGetMock,
  redirect: redirectMock,
  getSession: getSessionMock,
  logServerEvent: logServerEventMock,
} = mocks;

import {
  getCurrentSession,
  requireAdmin,
  requireAdminForAction,
  requireSession,
  requireSessionForAction,
} from "./guards";

beforeEach(() => {
  vi.clearAllMocks();
  headersGetMock.mockReturnValue(null);
});

const adminSession = { user: { id: "u1", role: "admin" } };
const userSession = { user: { id: "u2", role: "user" } };

describe("getCurrentSession", () => {
  it("delegates to auth.api.getSession with awaited headers", async () => {
    getSessionMock.mockResolvedValue(adminSession);
    const result = await getCurrentSession();
    expect(getSessionMock).toHaveBeenCalledTimes(1);
    const arg = getSessionMock.mock.calls[0]?.[0] as { headers: unknown };
    // headers() は HeadersLike を返す (get メソッドが生えている)
    expect(arg.headers).toBeDefined();
    expect(typeof (arg.headers as { get: unknown }).get).toBe("function");
    expect(result).toBe(adminSession);
  });

  it("returns null when no session", async () => {
    getSessionMock.mockResolvedValue(null);
    expect(await getCurrentSession()).toBeNull();
  });

  it("logs slow auth session lookup without exposing user details", async () => {
    const nowSpy = vi
      .spyOn(performance, "now")
      .mockReturnValueOnce(0)
      .mockReturnValueOnce(1601);
    try {
      getSessionMock.mockResolvedValue(adminSession);
      expect(await getCurrentSession()).toBe(adminSession);
      expect(logServerEventMock).toHaveBeenCalledWith(
        "warn",
        "frontend_auth_session_slow",
        { elapsedMs: 1601, hasSession: true },
      );
    } finally {
      nowSpy.mockRestore();
    }
  });

  it("logs auth session errors with error name only", async () => {
    getSessionMock.mockRejectedValue(new Error("database.example.internal"));

    await expect(getCurrentSession()).rejects.toThrow(
      "database.example.internal",
    );
    expect(logServerEventMock).toHaveBeenCalledWith(
      "error",
      "frontend_auth_session_error",
      { detail: "Error" },
    );
    expect(logServerEventMock).not.toHaveBeenCalledWith(
      "error",
      "frontend_auth_session_error",
      { detail: "database.example.internal" },
    );
  });
});

describe("requireSession", () => {
  it("redirects to /auth/login when session is null", async () => {
    getSessionMock.mockResolvedValue(null);
    await expect(requireSession()).rejects.toThrow("REDIRECT:/auth/login");
    expect(redirectMock).toHaveBeenCalledWith("/auth/login");
  });

  it("returns session when authenticated", async () => {
    getSessionMock.mockResolvedValue(adminSession);
    const result = await requireSession();
    expect(result).toBe(adminSession);
    expect(redirectMock).not.toHaveBeenCalled();
  });
});

describe("requireAdmin", () => {
  it("redirects to / when role is not admin", async () => {
    getSessionMock.mockResolvedValue(userSession);
    await expect(requireAdmin()).rejects.toThrow("REDIRECT:/");
    expect(redirectMock).toHaveBeenCalledWith("/");
  });

  it("returns session when role is admin", async () => {
    getSessionMock.mockResolvedValue(adminSession);
    expect(await requireAdmin()).toBe(adminSession);
  });

  it("redirects to /auth/login when no session (delegates to requireSession)", async () => {
    getSessionMock.mockResolvedValue(null);
    await expect(requireAdmin()).rejects.toThrow("REDIRECT:/auth/login");
  });

  it("downgrades unknown role string to 'user' (case-sensitive allowlist)", async () => {
    getSessionMock.mockResolvedValue({ user: { id: "u3", role: "Admin" } });
    await expect(requireAdmin()).rejects.toThrow("REDIRECT:/");
  });
});

describe("requireSessionForAction", () => {
  it("redirects to /auth/login when no session and no referer", async () => {
    getSessionMock.mockResolvedValue(null);
    headersGetMock.mockReturnValue(null);
    await expect(requireSessionForAction()).rejects.toThrow(
      "REDIRECT:/auth/login",
    );
  });

  it("redirects to /auth/login?callbackUrl=... when referer is an internal path", async () => {
    getSessionMock.mockResolvedValue(null);
    headersGetMock.mockReturnValue("https://example.com/watchlist");
    await expect(requireSessionForAction()).rejects.toThrow(
      "REDIRECT:/auth/login?callbackUrl=%2Fwatchlist",
    );
  });

  it("falls back to /auth/login when referer is /auth/* (loop prevention)", async () => {
    getSessionMock.mockResolvedValue(null);
    headersGetMock.mockReturnValue("https://example.com/auth/login");
    await expect(requireSessionForAction()).rejects.toThrow(
      "REDIRECT:/auth/login",
    );
  });

  it("returns session when authenticated (no redirect)", async () => {
    getSessionMock.mockResolvedValue(userSession);
    expect(await requireSessionForAction()).toBe(userSession);
    expect(redirectMock).not.toHaveBeenCalled();
  });
});

describe("requireAdminForAction", () => {
  it("throws Error('Forbidden') for logged-in non-admin", async () => {
    getSessionMock.mockResolvedValue(userSession);
    await expect(requireAdminForAction()).rejects.toThrow("Forbidden");
    expect(redirectMock).not.toHaveBeenCalled();
  });

  it("returns session when admin", async () => {
    getSessionMock.mockResolvedValue(adminSession);
    expect(await requireAdminForAction()).toBe(adminSession);
  });

  it("redirects via requireSessionForAction when no session", async () => {
    getSessionMock.mockResolvedValue(null);
    headersGetMock.mockReturnValue(null);
    await expect(requireAdminForAction()).rejects.toThrow(
      "REDIRECT:/auth/login",
    );
  });

  it("treats casing-mismatched role as non-admin (Forbidden)", async () => {
    getSessionMock.mockResolvedValue({ user: { id: "u4", role: "ADMIN" } });
    await expect(requireAdminForAction()).rejects.toThrow("Forbidden");
  });
});
