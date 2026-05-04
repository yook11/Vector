import { describe, expect, it } from "vitest";
import { buildIdentifier, extractClientIp } from "./identifier";

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
  it("returns ip kind keyed by the first XFF value", () => {
    expect(buildIdentifier("203.0.113.1, 10.0.0.1", null)).toEqual({
      kind: "ip",
      key: "203.0.113.1",
    });
  });

  it("falls back to x-real-ip when XFF is empty", () => {
    expect(buildIdentifier(null, "198.51.100.5")).toEqual({
      kind: "ip",
      key: "198.51.100.5",
    });
  });

  it("falls back to 'unknown' bucket when no IP can be extracted", () => {
    // identifier null fail-closed (red-team F2 対策)。XFF / X-Real-IP 両欠如の
    // 非正規 request は "unknown" bucket に集約され throttle 対象になる。
    expect(buildIdentifier(null, null)).toEqual({
      kind: "ip",
      key: "unknown",
    });
  });

  it("treats whitespace-only headers as missing and falls back to 'unknown'", () => {
    expect(buildIdentifier("   ", "   ")).toEqual({
      kind: "ip",
      key: "unknown",
    });
  });
});
