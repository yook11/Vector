import { describe, expect, it } from "vitest";
import { getCategoryKicker } from "./paper-style";

describe("getCategoryKicker", () => {
  it("maps a known slug to its English code and hue", () => {
    const kicker = getCategoryKicker("security");
    expect(kicker.code).toBe("SECURITY");
    expect(kicker.hue).toBe("#C2562F");
  });

  it("maps the 'other' slug to the MARKET code", () => {
    expect(getCategoryKicker("other").code).toBe("MARKET");
  });

  it("falls back to NEWS for an unknown slug", () => {
    expect(getCategoryKicker("unknown-slug").code).toBe("NEWS");
  });

  it("derives a lighter dark-mode hue", () => {
    const { hue, hueDark } = getCategoryKicker("ai");
    expect(hueDark).not.toBe(hue);
    const channelSum = (hex: string) =>
      Number.parseInt(hex.slice(1, 3), 16) +
      Number.parseInt(hex.slice(3, 5), 16) +
      Number.parseInt(hex.slice(5, 7), 16);
    expect(channelSum(hueDark)).toBeGreaterThan(channelSum(hue));
  });
});
