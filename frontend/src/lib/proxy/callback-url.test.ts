import { describe, expect, it } from "vitest";
import { isInternalPath, sanitizeCallbackUrl } from "./callback-url";

describe("isInternalPath", () => {
  it("accepts a single-segment internal path", () => {
    expect(isInternalPath("/foo")).toBe(true);
  });

  it("accepts the root '/'", () => {
    expect(isInternalPath("/")).toBe(true);
  });

  it("accepts internal paths with query string", () => {
    expect(isInternalPath("/watchlist?sort=desc")).toBe(true);
  });

  it("rejects protocol-relative URLs (open redirect vector)", () => {
    expect(isInternalPath("//evil.com")).toBe(false);
    expect(isInternalPath("//evil.com/path")).toBe(false);
  });

  it("rejects absolute URLs", () => {
    expect(isInternalPath("http://evil.com")).toBe(false);
    expect(isInternalPath("https://evil.com/path")).toBe(false);
  });

  it("rejects empty string", () => {
    expect(isInternalPath("")).toBe(false);
  });

  it("rejects paths without leading slash", () => {
    expect(isInternalPath("foo")).toBe(false);
    expect(isInternalPath("foo/bar")).toBe(false);
  });
});

describe("sanitizeCallbackUrl", () => {
  it("returns the path unchanged when internal", () => {
    expect(sanitizeCallbackUrl("/dashboard")).toBe("/dashboard");
  });

  it("preserves query string on internal path", () => {
    expect(sanitizeCallbackUrl("/news?category=ai&page=2")).toBe(
      "/news?category=ai&page=2",
    );
  });

  it("returns null for protocol-relative URL", () => {
    expect(sanitizeCallbackUrl("//evil.com")).toBeNull();
  });

  it("returns null for absolute URL", () => {
    expect(sanitizeCallbackUrl("https://evil.com/path")).toBeNull();
  });

  it("returns null for empty string", () => {
    expect(sanitizeCallbackUrl("")).toBeNull();
  });
});
