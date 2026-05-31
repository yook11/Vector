import { afterEach, describe, expect, it, vi } from "vitest";
import { buildCspDirectives, buildCspHeader, generateNonce } from "./csp";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("generateNonce", () => {
  it("returns a base64 string", () => {
    const nonce = generateNonce();
    expect(typeof nonce).toBe("string");
    expect(nonce.length).toBeGreaterThan(0);
    // base64 alphabet (RFC 4648 standard, with optional '=' padding)
    expect(nonce).toMatch(/^[A-Za-z0-9+/]+=*$/);
  });

  it("encodes at least 16 bytes of entropy (UUID has 36 chars → base64 >= 24)", () => {
    const nonce = generateNonce();
    // base64 of a 36-char UUID = 48 chars (with padding stripped/included)
    expect(nonce.length).toBeGreaterThanOrEqual(24);
  });

  it("returns a different value on each call (uses crypto.randomUUID)", () => {
    const nonces = new Set([
      generateNonce(),
      generateNonce(),
      generateNonce(),
      generateNonce(),
      generateNonce(),
    ]);
    expect(nonces.size).toBe(5);
  });

  it("uses globalThis.crypto.randomUUID (mockable for determinism)", () => {
    const spy = vi
      .spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValue("00000000-0000-0000-0000-000000000000");
    const nonce = generateNonce();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(nonce).toBe(
      Buffer.from("00000000-0000-0000-0000-000000000000").toString("base64"),
    );
  });
});

describe("buildCspDirectives", () => {
  it("returns 9 directives in the documented order", () => {
    const directives = buildCspDirectives("test-nonce", false);
    expect(directives).toHaveLength(9);
    expect(directives[0]).toBe("default-src 'self'");
    expect(directives[2]).toBe("style-src 'self' 'unsafe-inline'");
    expect(directives[3]).toBe("img-src 'self' data:");
    expect(directives[4]).toBe("font-src 'self'");
    expect(directives[5]).toBe("connect-src 'self'");
    expect(directives[6]).toBe("frame-ancestors 'none'");
    expect(directives[7]).toBe("form-action 'self'");
    expect(directives[8]).toBe("base-uri 'self'");
  });

  it("keeps nonce for inline scripts without strict-dynamic", () => {
    const directives = buildCspDirectives("abc123", false);
    expect(directives[1]).toBe(
      "script-src 'self' 'nonce-abc123' 'sha256-7mu4H06fwDCjmnxxr/xNHyuQC6pLTHr4M2E4jXw5WZs='",
    );
    expect(directives[1]).not.toContain("'strict-dynamic'");
  });

  it("adds 'unsafe-eval' in script-src when isDev=true (HMR support)", () => {
    const directives = buildCspDirectives("abc", true);
    expect(directives[1]).toContain("'unsafe-eval'");
  });

  it("does NOT add 'unsafe-eval' when isDev=false (production hardening)", () => {
    const directives = buildCspDirectives("abc", false);
    expect(directives[1]).not.toContain("'unsafe-eval'");
  });

  it("includes the clickjacking-defense directives", () => {
    const directives = buildCspDirectives("n", false);
    expect(directives).toContain("frame-ancestors 'none'");
    expect(directives).toContain("form-action 'self'");
    expect(directives).toContain("base-uri 'self'");
  });
});

describe("buildCspHeader", () => {
  it("joins directives with '; '", () => {
    expect(buildCspHeader(["a", "b", "c"])).toBe("a; b; c");
  });

  it("returns empty string for empty input", () => {
    expect(buildCspHeader([])).toBe("");
  });

  it("composes a valid header from buildCspDirectives output", () => {
    const header = buildCspHeader(buildCspDirectives("xyz", false));
    expect(header).toContain("default-src 'self'");
    expect(header).toContain("script-src 'self' 'nonce-xyz'");
    expect(header).toContain(
      "sha256-7mu4H06fwDCjmnxxr/xNHyuQC6pLTHr4M2E4jXw5WZs=",
    );
    expect(header).toContain("frame-ancestors 'none'");
    // separator must be "; " (CSP grammar)
    expect(header.split("; ")).toHaveLength(9);
  });
});
