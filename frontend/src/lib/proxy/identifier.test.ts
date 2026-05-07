import { describe, expect, it } from "vitest";
import { buildIdentifier, extractClientIp } from "./identifier";

// extractClientIp は (flyClientIp, forwardedFor, realIp, isProduction) の
// 4 引数。production では Fly-Client-IP のみ trusted、欠如時は fail-closed で
// null を返す。development では Fly-Client-IP → x-forwarded-for → x-real-ip
// の fallback chain を辿る (PR10 / red-team C1 / F2-F4 構造防御)。

describe("extractClientIp — production (Fly-Client-IP only)", () => {
  it("prefers Fly-Client-IP when present", () => {
    expect(extractClientIp("203.0.113.10", null, null, true)).toBe(
      "203.0.113.10",
    );
  });

  it("preserves IPv6 Fly-Client-IP", () => {
    expect(extractClientIp("2001:db8::1", null, null, true)).toBe(
      "2001:db8::1",
    );
  });

  it("trims whitespace from Fly-Client-IP", () => {
    expect(extractClientIp("  203.0.113.10  ", null, null, true)).toBe(
      "203.0.113.10",
    );
  });

  it("returns null when Fly-Client-IP is absent (fail-closed)", () => {
    // Fly proxy bypass / Fly Edge 設定崩壊の異常経路。x-forwarded-for / x-real-ip
    // が来ていても production では使わない (詐称された値を信頼しない構造保証)。
    expect(
      extractClientIp(null, "203.0.113.99", "198.51.100.99", true),
    ).toBeNull();
  });

  it("returns null even when x-forwarded-for is spoofable in production", () => {
    // 攻撃者が x-forwarded-for を詐称しても production では fail-closed。
    expect(extractClientIp(null, "1.2.3.4, 5.6.7.8", null, true)).toBeNull();
  });

  it("returns null when Fly-Client-IP is empty string in production", () => {
    expect(extractClientIp("", "203.0.113.99", null, true)).toBeNull();
  });

  it("returns null when Fly-Client-IP is whitespace-only in production", () => {
    expect(extractClientIp("   ", "203.0.113.99", null, true)).toBeNull();
  });
});

describe("extractClientIp — development (Fly-Client-IP + fallback)", () => {
  it("prefers Fly-Client-IP when present in development", () => {
    expect(extractClientIp("203.0.113.10", "1.2.3.4", null, false)).toBe(
      "203.0.113.10",
    );
  });

  it("falls back to x-forwarded-for first value when Fly-Client-IP is absent", () => {
    expect(
      extractClientIp(null, "203.0.113.1, 198.51.100.1, 10.0.0.1", null, false),
    ).toBe("203.0.113.1");
  });

  it("trims whitespace from x-forwarded-for first value", () => {
    expect(
      extractClientIp(null, "  203.0.113.1  , 10.0.0.1", null, false),
    ).toBe("203.0.113.1");
  });

  it("preserves IPv6 in x-forwarded-for fallback", () => {
    expect(extractClientIp(null, "2001:db8::1", null, false)).toBe(
      "2001:db8::1",
    );
  });

  it("falls back to x-real-ip when Fly-Client-IP and x-forwarded-for are null", () => {
    expect(extractClientIp(null, null, "203.0.113.2", false)).toBe(
      "203.0.113.2",
    );
  });

  it("falls back to x-real-ip when Fly-Client-IP is null and x-forwarded-for is empty", () => {
    expect(extractClientIp(null, "", "203.0.113.2", false)).toBe("203.0.113.2");
  });

  it("returns null when all sources are absent in development", () => {
    expect(extractClientIp(null, null, null, false)).toBeNull();
  });

  it("returns null when all sources are whitespace-only in development", () => {
    expect(extractClientIp("   ", "   ", "   ", false)).toBeNull();
  });
});

describe("buildIdentifier", () => {
  it("returns ip kind keyed by Fly-Client-IP in production", () => {
    expect(buildIdentifier("203.0.113.10", null, null, true)).toEqual({
      kind: "ip",
      key: "203.0.113.10",
    });
  });

  it("falls back to 'unknown' bucket when Fly-Client-IP is missing in production", () => {
    // production fail-closed で全異常 request が "unknown" bucket に集約される
    // (red-team C1 / F2-F4)。攻撃者は x-forwarded-for を詐称しても
    // 自分専用の bucket を生成できず、他の異常 request 群と共倒れする。
    expect(buildIdentifier(null, "1.2.3.4, 5.6.7.8", null, true)).toEqual({
      kind: "ip",
      key: "unknown",
    });
  });

  it("returns ip kind keyed by the first XFF value in development", () => {
    expect(buildIdentifier(null, "203.0.113.1, 10.0.0.1", null, false)).toEqual(
      {
        kind: "ip",
        key: "203.0.113.1",
      },
    );
  });

  it("falls back to x-real-ip when Fly-Client-IP and XFF are absent in development", () => {
    expect(buildIdentifier(null, null, "198.51.100.5", false)).toEqual({
      kind: "ip",
      key: "198.51.100.5",
    });
  });

  it("falls back to 'unknown' bucket when no IP source is present in development", () => {
    // identifier null fail-closed (red-team F2 対策)。Fly-Client-IP / XFF /
    // X-Real-IP 全欠如の非正規 request は "unknown" bucket に集約され
    // throttle 対象になる。
    expect(buildIdentifier(null, null, null, false)).toEqual({
      kind: "ip",
      key: "unknown",
    });
  });

  it("treats whitespace-only headers as missing and falls back to 'unknown' in development", () => {
    expect(buildIdentifier("   ", "   ", "   ", false)).toEqual({
      kind: "ip",
      key: "unknown",
    });
  });
});
