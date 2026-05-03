import { describe, expect, it } from "vitest";
import {
  buildIdentifier,
  extractClientIp,
  hashSessionCookie,
} from "./identifier";

describe("hashSessionCookie", () => {
  it("returns 16-char hex hash", () => {
    const result = hashSessionCookie("session-token-value");
    expect(result).toMatch(/^[0-9a-f]{16}$/);
  });

  it("is deterministic", () => {
    const a = hashSessionCookie("token");
    const b = hashSessionCookie("token");
    expect(a).toBe(b);
  });

  it("differs for different inputs", () => {
    expect(hashSessionCookie("a")).not.toBe(hashSessionCookie("b"));
  });
});

describe("extractClientIp", () => {
  it("picks the first value of x-forwarded-for", () => {
    expect(extractClientIp("203.0.113.1, 198.51.100.1, 10.0.0.1", null)).toBe(
      "203.0.113.1",
    );
  });

  it("trims whitespace from the first value", () => {
    expect(extractClientIp("  203.0.113.1  , 10.0.0.1", null)).toBe(
      "203.0.113.1",
    );
  });

  it("preserves IPv6 addresses", () => {
    expect(extractClientIp("2001:db8::1", null)).toBe("2001:db8::1");
  });

  it("falls back to x-real-ip when forwarded-for is null", () => {
    expect(extractClientIp(null, "203.0.113.2")).toBe("203.0.113.2");
  });

  it("falls back to x-real-ip when forwarded-for is empty", () => {
    expect(extractClientIp("", "203.0.113.2")).toBe("203.0.113.2");
  });

  it("returns null when both are absent", () => {
    expect(extractClientIp(null, null)).toBeNull();
  });

  it("returns null when both are empty/whitespace", () => {
    expect(extractClientIp("   ", "   ")).toBeNull();
  });
});

describe("buildIdentifier", () => {
  it("uses cookie hash when session cookie is present", () => {
    const result = buildIdentifier("token-value", "203.0.113.1", null);
    expect(result).toEqual({
      kind: "auth",
      key: hashSessionCookie("token-value"),
    });
  });

  it("ignores empty cookie and falls back to IP", () => {
    expect(buildIdentifier("   ", "203.0.113.1", null)).toEqual({
      kind: "anon",
      key: "203.0.113.1",
    });
  });

  it("returns anon kind with IP when no cookie", () => {
    expect(buildIdentifier(null, "203.0.113.1", null)).toEqual({
      kind: "anon",
      key: "203.0.113.1",
    });
  });

  it("returns null when neither cookie nor IP is available", () => {
    expect(buildIdentifier(null, null, null)).toBeNull();
  });

  it("does not leak raw cookie value in the identifier", () => {
    const result = buildIdentifier("super-secret-token", null, null);
    expect(result).not.toBeNull();
    if (result) {
      expect(result.key).not.toContain("super-secret-token");
      expect(result.key).toHaveLength(16);
    }
  });
});
