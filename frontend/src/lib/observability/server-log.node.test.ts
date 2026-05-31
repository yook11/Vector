import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { logServerEvent } from "./server-log";

const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

beforeEach(() => {
  warnSpy.mockClear();
  errorSpy.mockClear();
});

afterAll(() => {
  warnSpy.mockRestore();
  errorSpy.mockRestore();
});

describe("logServerEvent", () => {
  it("writes a single-line JSON warn event with allowed fields", () => {
    logServerEvent("warn", "frontend_internal_api_failure", {
      kind: "http_429",
      method: "GET",
      path: "/api/v1/articles",
      status: 429,
      detail: "Too Many Requests",
    });

    expect(warnSpy).toHaveBeenCalledTimes(1);
    const line = warnSpy.mock.calls[0]?.[0];
    expect(typeof line).toBe("string");
    expect(line).not.toContain("\n");
    expect(JSON.parse(line as string)).toEqual({
      event: "frontend_internal_api_failure",
      level: "warn",
      kind: "http_429",
      method: "GET",
      path: "/api/v1/articles",
      status: 429,
      detail: "Too Many Requests",
    });
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("strips query strings from path", () => {
    logServerEvent("error", "frontend_internal_api_failure", {
      path: "/api/v1/articles?search=secret",
      kind: "network",
    });

    const line = errorSpy.mock.calls[0]?.[0];
    expect(JSON.parse(line as string)).toMatchObject({
      path: "/api/v1/articles",
      kind: "network",
    });
  });
});
